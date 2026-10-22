/*
 * SoilSense Control: week 4, the full migration to kernel threads.
 *
 * Starting point: the week-3 sampling-thread build (superloop + the patch in
 * evidencia/lab03/c6/lab03-task-c.patch). Every task that still lived in the
 * loop now has its own thread or deferred work, and main() only brings the
 * hardware up, starts the timer and returns.
 *
 *   sampling   (hard, 1 kHz)  thread, prio 2, woken by tick_q (k_msgq)
 *   control    (hard, 10 ms)  thread, prio 3, woken by control_sem (k_sem)
 *   flow batch (hard)         k_work on flow_wq, prio 5, every 100 pulses
 *   display    (soft)         thread, prio 7, 50 ms sleep
 *   telemetry  (soft, 1 Hz)   thread, prio 8, absolute 1 s sleep
 *   console    (firm)         thread, prio 9, 5 ms sleep between polls
 *
 * The task bodies and the instrumentation GPIOs are the week-3 ones, unchanged,
 * so the A/B against the superloop compares architectures and nothing else.
 *
 * Week 6 adds, without changing the plan above:
 *   Task B  control's PI state behind one lock shared with the console;
 *           `lock sem|mutex` picks its kind, `invert` forces the inversion,
 *           instr_lock (GPIO18) is high while the console holds it
 *   Task C  instr_flow marks the e-stop detection; CONFIG_LAB_ADR005
 *           (adr005.conf) is the "after" build of ADR-005
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/util.h>
#include <zephyr/sys/printk.h>
#ifdef CONFIG_THREAD_ANALYZER
#include <zephyr/debug/thread_analyzer.h>
#endif
#ifdef CONFIG_CHARACTER_FRAMEBUFFER
#include <zephyr/drivers/display.h>
#include <zephyr/display/cfb.h>
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SAMPLE_PERIOD_US 1000  /* sampling: 1 kHz */
#define CONTROL_EVERY    10    /* control runs every 10th sample -> 10 ms */
#define TELEMETRY_MS     1000
#define PWM_STEPS        20    /* software PWM on the valve LED: 1 kHz / 20 = 50 Hz */
#define FLOW_BATCH       100   /* pulses accumulated before the batch math runs */
#define ESTOP_MV         3000  /* overpressure threshold */
#define CALIB_ROUNDS     1000  /* `calib` command: rounds of read + settle */
#define DISPLAY_MS       500   /* HMI refresh period */
#define ESTOP_HOLD       50    /* joystick-down samples (at 50 Hz) = 1 s hold */
#define INV_ROUNDS       5     /* `invert`: inversions forced per command */
#define INV_CS_US        1000  /* `invert`: the console's critical section, B */
#define INV_MEDIUM_US    20000 /* `invert`: telemetry's busy work, woken inside it */

/* Thread plan, rate-monotonic: the shorter the period, the higher the priority.
 * main keeps the default priority 0, above all of them, so init_hw() finishes
 * before any thread touches a pin. */
#define SAMPLING_PRIO   2
#define CONTROL_PRIO    3
#define FLOW_WQ_PRIO    5
#define DISPLAY_PRIO    7
#define TELEMETRY_PRIO  8
#define CONSOLE_PRIO    9

#define SAMPLING_STACK  1536
#define CONTROL_STACK   1024
#define FLOW_WQ_STACK   1024
#define DISPLAY_STACK   2048
#define TELEMETRY_STACK 2048
#define CONSOLE_STACK   2048

#define DISPLAY_POLL_MS 50
#define CONSOLE_POLL_MS 5      /* 128-byte RX FIFO = ~11 ms of typing at 115200 */

/* ---- Hardware (all optional: missing DT nodes degrade to a synthetic rig) -- */

#define INSTR(node) GPIO_DT_SPEC_GET_OR(DT_NODELABEL(node), gpios, {0})
static const struct gpio_dt_spec instr_samp = INSTR(instr_samp);
static const struct gpio_dt_spec instr_ctrl = INSTR(instr_ctrl);
static const struct gpio_dt_spec instr_cons = INSTR(instr_cons);
static const struct gpio_dt_spec instr_tele = INSTR(instr_tele);
static const struct gpio_dt_spec instr_flow = INSTR(instr_flow);
static const struct gpio_dt_spec instr_disp = INSTR(instr_disp);
static const struct gpio_dt_spec instr_lock = INSTR(instr_lock);
static const struct gpio_dt_spec valve_led =
	GPIO_DT_SPEC_GET_OR(DT_ALIAS(led0), gpios, {0});
static const struct gpio_dt_spec flow_pulse =
	GPIO_DT_SPEC_GET_OR(DT_NODELABEL(flow_pulse), gpios, {0});

#if DT_NODE_EXISTS(DT_PATH(zephyr_user)) && \
	DT_NODE_HAS_PROP(DT_PATH(zephyr_user), io_channels)
#define HAS_ADC 1
static const struct adc_dt_spec adc_joy =
	ADC_DT_SPEC_GET_BY_IDX(DT_PATH(zephyr_user), 0);
static const struct adc_dt_spec adc_pressure =
	ADC_DT_SPEC_GET_BY_IDX(DT_PATH(zephyr_user), 1);
#endif

#if DT_HAS_CHOSEN(zephyr_console)
static const struct device *const console_uart =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
#endif

#ifdef CONFIG_CHARACTER_FRAMEBUFFER
static const struct device *const disp = DEVICE_DT_GET_ANY(solomon_ssd1306);
#endif

static inline void instr_set(const struct gpio_dt_spec *p, int v)
{
	if (p->port) {
		gpio_pin_set_dt(p, v);
	}
}

/* ---- Kernel objects ------------------------------------------------------- */

/* Sampling tick: the ISR timestamps the release and hands it to the sampling
 * thread through a queue (it carries data: the release time). */
K_MSGQ_DEFINE(tick_q, sizeof(uint32_t), 8, 4);

/* Control only needs a "go", so a semaphore. Its limit is 1 on purpose: if a
 * give finds the count already at 1, the previous release never even started,
 * which is a deadline miss of REQ-CTRL-01 (D = T = 10 ms). It is counted
 * instead of queued, so control never runs twice back to back on stale data. */
K_SEM_DEFINE(control_sem, 0, 1);

/* The flow batch goes to a workqueue of our own. The system workqueue runs at
 * -1 (cooperative), and sampling could not preempt a batch running there. */
K_THREAD_STACK_DEFINE(flow_wq_stack, FLOW_WQ_STACK);
static struct k_work_q flow_wq;
static struct k_work flow_work;

static atomic_t backlog_peak;   /* worst tick_q occupancy, saturates at 8 */
static atomic_t lat_peak_us;    /* worst release -> sampling thread start */
static atomic_t ticks_dropped;  /* releases rejected by the full queue */
static atomic_t ctrl_missed;    /* control releases that found the last one pending */

/* Week 6 Task B: control's PI state (setpoint, integral) is shared with the
 * console, so both take one lock around it. `lock sem|mutex` picks its kind at
 * run time, and the three regimes of Task B run on one build:
 *   mutex  k_mutex, the holder inherits the waiter's priority (default, ADR-002)
 *   sem    k_sem of 1, no inheritance: the priority inversion
 * A take returns the kind it used, so a switch never unlocks the wrong one. */
enum lock_kind { LOCK_MUTEX, LOCK_SEM };
static enum lock_kind lock_kind = LOCK_MUTEX;
K_MUTEX_DEFINE(pi_mutex);
K_SEM_DEFINE(pi_sem, 1, 1);

static enum lock_kind pi_lock(void)
{
	enum lock_kind k = lock_kind;

	if (k == LOCK_SEM) {
		k_sem_take(&pi_sem, K_FOREVER);
	} else {
		k_mutex_lock(&pi_mutex, K_FOREVER);
	}
	return k;
}

static void pi_unlock(enum lock_kind k)
{
	if (k == LOCK_SEM) {
		k_sem_give(&pi_sem);
	} else {
		k_mutex_unlock(&pi_mutex);
	}
}

static atomic_t ctrl_released;  /* tick time of control's latest release (cycles) */
static atomic_t ctrl_r_peak_us; /* worst control response, release -> end */
static atomic_t ctrl_waiting;   /* control is asking for the PI lock */
static atomic_t cs_peak_us;     /* longest console hold of the PI lock: B */
static atomic_t medium_kick;    /* `invert` asks telemetry for its busy work */

/* ---- ISR side ------------------------------------------------------------- */

static void tick_isr(struct k_timer *t)
{
	uint32_t released = k_cycle_get_32();

	if (k_msgq_put(&tick_q, &released, K_NO_WAIT) != 0) {
		atomic_inc(&ticks_dropped);
	}

	atomic_val_t backlog = k_msgq_num_used_get(&tick_q);

	if (backlog > atomic_get(&backlog_peak)) {
		atomic_set(&backlog_peak, backlog);
	}
}
K_TIMER_DEFINE(tick_timer, tick_isr, NULL);

/* Flow pulses: counted in the ISR; the 100th one submits the batch. */
static atomic_t flow_pulses;
static struct gpio_callback flow_cb;

static void flow_isr(const struct device *dev, struct gpio_callback *cb,
		     uint32_t pins)
{
	if (atomic_inc(&flow_pulses) + 1 >= FLOW_BATCH) {
		k_work_submit_to_queue(&flow_wq, &flow_work);
	}
}

/* ---- State ---------------------------------------------------------------- */

static int setpoint_mv = 1500;
static int pressure_mv;
static int joystick_mv = -1;
static int duty_pct;          /* valve command, 0-100 */
static int integral_mv;
static bool estop;
static float flow_lpm;        /* batch math result (float on purpose: no FPU) */
static uint32_t flow_batches;

enum page { PAGE_DASH, PAGE_FLOW, PAGE_HEALTH, PAGE_COUNT };
static int page;
static bool page_dirty = true;

/* ---- Inputs: ADC when the board wires it, a synthetic plant when not ------ */

#if HAS_ADC
static int adc_read_mv(const struct adc_dt_spec *spec)
{
	int16_t raw;
	struct adc_sequence seq = { .buffer = &raw, .buffer_size = sizeof(raw) };
	int32_t mv;

	adc_sequence_init_dt(spec, &seq);
	if (adc_read_dt(spec, &seq) < 0) {
		return -1;
	}
	mv = raw;
	adc_raw_to_millivolts_dt(spec, &mv);
	return mv;
}
#endif

/* Without a pot on the pressure channel (or on native_sim) the plant is
 * simulated: pressure drifts toward what the valve commands. */
static int plant_mv(void)
{
	static int mv = 1200;

	mv += (duty_pct * 33 - mv) / 64;
	mv += (int)(k_cycle_get_32() % 7) - 3;
	return mv;
}

static int read_pressure_mv(void)
{
#if HAS_ADC
	int mv = adc_read_mv(&adc_pressure);

	if (mv >= 0) {
		return mv;
	}
#endif
	return plant_mv();
}

static int read_joystick_mv(void)
{
#if HAS_ADC
	return adc_read_mv(&adc_joy);
#else
	return -1;
#endif
}

/* ---- Tasks (bodies unchanged from weeks 2 and 3) -------------------------- */

/* Joystick bands, single-ADC resistor ladder. Calibrate with `joy`. */
enum joy { JOY_NONE, JOY_UP, JOY_DOWN, JOY_LEFT, JOY_RIGHT, JOY_SEL };

static enum joy joy_decode(int mv)
{
	if (mv < 0) return JOY_NONE;
	if (mv < 300) return JOY_SEL;
	if (mv < 900) return JOY_DOWN;
	if (mv < 1600) return JOY_RIGHT;
	if (mv < 2300) return JOY_LEFT;
	if (mv < 3000) return JOY_UP;
	return JOY_NONE;
}

/* Joystick UX, sampled at 50 Hz: left/right turn HMI pages, up/down move the
 * setpoint on the dashboard, down *held* ~1 s is the e-stop, center clears it. */
static void joystick_update(void)
{
	static enum joy prev = JOY_NONE;
	static int down_held;

	joystick_mv = read_joystick_mv();
	enum joy j = joy_decode(joystick_mv);

	if (j == JOY_DOWN) {
		if (++down_held == ESTOP_HOLD) {
			estop = true;
			page_dirty = true;
		}
	} else {
		down_held = 0;
	}

	switch (j) {
	case JOY_UP: /* held = ramp */
		if (page == PAGE_DASH) {
			setpoint_mv = CLAMP(setpoint_mv + 10, 0, 3300);
			page_dirty = true;
		}
		break;
	case JOY_DOWN: /* single step per press; keep holding for e-stop */
		if (page == PAGE_DASH && j != prev) {
			setpoint_mv = CLAMP(setpoint_mv - 10, 0, 3300);
			page_dirty = true;
		}
		break;
	case JOY_LEFT:
		if (j != prev) {
			page = (page + PAGE_COUNT - 1) % PAGE_COUNT;
			page_dirty = true;
		}
		break;
	case JOY_RIGHT:
		if (j != prev) {
			page = (page + 1) % PAGE_COUNT;
			page_dirty = true;
		}
		break;
	case JOY_SEL:
		estop = false;
		integral_mv = 0;
		page_dirty = true;
		break;
	default:
		break;
	}
	prev = j;
}

static void task_sampling(void)
{
	static int pwm_phase;
	static int joy_div;

	instr_set(&instr_samp, 1);

	pressure_mv = read_pressure_mv();

	/* e-stop condition is *detected* here, at 1 kHz. As shipped the valve only
	 * reacts when control runs; with ADR-005 the PWM step below drives it low
	 * in this same job. */
	if (pressure_mv > ESTOP_MV) {
		estop = true;
#ifdef CONFIG_LAB_ADR005
		duty_pct = 0;
#endif
	}
	/* Week 6 Task C: instr_flow marks the detection. The flow input stays
	 * unplugged, so the batch never drives this pin as well. */
	instr_set(&instr_flow, estop);

	/* joystick, decimated to 50 Hz */
	if (++joy_div >= 20) {
		joy_div = 0;
		joystick_update();
	}

	/* software PWM on the valve */
	pwm_phase = (pwm_phase + 1) % PWM_STEPS;
	if (valve_led.port) {
		gpio_pin_set_dt(&valve_led,
				pwm_phase < (duty_pct * PWM_STEPS) / 100);
	}

	instr_set(&instr_samp, 0);
}

static void task_control(void)
{
	instr_set(&instr_ctrl, 1);

	/* Week 6 Task B: the PI state is shared with the console. instr_ctrl rises
	 * before the lock, so a wait for it shows inside control's pulse. */
	atomic_set(&ctrl_waiting, 1);
	enum lock_kind k = pi_lock();

	atomic_set(&ctrl_waiting, 0);

	if (estop) {
		duty_pct = 0;
	} else {
		int error = setpoint_mv - pressure_mv;

		integral_mv += error / 8;
		integral_mv = CLAMP(integral_mv, -2000, 2000);
		duty_pct = CLAMP((error + integral_mv) / 20, 0, 100);
	}

	pi_unlock(k);
	instr_set(&instr_ctrl, 0);
}

/* Every FLOW_BATCH pulses: convert the accumulated count to L/min and smooth
 * it. Float math on a core without an FPU, now off the loop's critical path. */
static void task_flow_batch(void)
{
	if (atomic_get(&flow_pulses) < FLOW_BATCH) {
		return;
	}
	instr_set(&instr_flow, 1);

	float pulses = (float)atomic_clear(&flow_pulses);
	float lpm = pulses / 5880.0f * 60.0f; /* YF-S401: 5880 pulses/L */

	for (int i = 0; i < 50; i++) {
		flow_lpm = flow_lpm * 0.9f + lpm * 0.1f;
	}
	flow_batches++;

	instr_set(&instr_flow, 0);
}

/* Same line, same length as weeks 2 and 3: the UART time it costs is part of
 * what the A/B compares. The thread now owns the 1 s timing. */
static void task_telemetry(void)
{
	char line[96];

	instr_set(&instr_tele, 1);

	snprintf(line, sizeof(line),
		 "t=%u p_mv=%d sp_mv=%d duty=%d flow_x100=%d estop=%d backlog=%ld",
		 (unsigned int)k_uptime_get(), pressure_mv, setpoint_mv, duty_pct,
		 (int)(flow_lpm * 100.0f), (int)estop,
		 (long)atomic_get(&backlog_peak));
	printk("%s\n", line);

	instr_set(&instr_tele, 0);
}

/* Week 6 Task B: the medium-priority busy work. `invert` wakes telemetry from
 * inside the console's critical section, the phasing that in the field only
 * happens by chance. Telemetry never touches the PI lock. */
static void task_medium(void)
{
	instr_set(&instr_tele, 1);
	k_busy_wait(INV_MEDIUM_US);
	instr_set(&instr_tele, 0);
}

/* ---- Display (soft "HMI"): SSD1306 pages, redrawn whole ------------------- */

#ifdef CONFIG_CHARACTER_FRAMEBUFFER
static void task_display(void)
{
	static int64_t next_ms;
	static bool ready;
	char line[24];

	if (disp == NULL) {
		return;
	}
	if (!ready) {
		if (!device_is_ready(disp) || cfb_framebuffer_init(disp) != 0) {
			return;
		}
		display_blanking_off(disp);
		ready = true;
	}
	if (!page_dirty && k_uptime_get() < next_ms) {
		return;
	}
	next_ms = k_uptime_get() + DISPLAY_MS;
	page_dirty = false;

	instr_set(&instr_disp, 1);
	cfb_framebuffer_clear(disp, false);
	switch (page) {
	case PAGE_DASH:
		snprintf(line, sizeof(line), "P %4d>%4dmV", pressure_mv,
			 setpoint_mv);
		cfb_print(disp, line, 0, 0);
		snprintf(line, sizeof(line), "duty %3d%% %s", duty_pct,
			 estop ? "ESTOP" : "");
		cfb_print(disp, line, 0, 16);
		cfb_print(disp, "1/3 dash", 0, 48);
		break;
	case PAGE_FLOW:
		snprintf(line, sizeof(line), "flow %d.%02d L/m",
			 (int)flow_lpm, (int)(flow_lpm * 100.0f) % 100);
		cfb_print(disp, line, 0, 0);
		snprintf(line, sizeof(line), "batches %u", flow_batches);
		cfb_print(disp, line, 0, 16);
		cfb_print(disp, "2/3 flow", 0, 48);
		break;
	case PAGE_HEALTH:
		snprintf(line, sizeof(line), "up %us",
			 (unsigned int)(k_uptime_get() / 1000));
		cfb_print(disp, line, 0, 0);
		snprintf(line, sizeof(line), "backlog pk %ld",
			 (long)atomic_get(&backlog_peak));
		cfb_print(disp, line, 0, 16);
		cfb_print(disp, "3/3 health", 0, 48);
		break;
	}
	/* the whole 1 KB frame goes over I2C here: blocking, but only this thread */
	cfb_framebuffer_finalize(disp);
	instr_set(&instr_disp, 0);
}
#else
static void task_display(void)
{
}
#endif

/* ---- Console (firm): poll chars, run a full command synchronously --------- */

static void cmd_calib(void)
{
	uint32_t t0 = k_uptime_get_32();
#ifdef CONFIG_SCHED_THREAD_USAGE
	/* Week 6 Task C: the console's own CPU time during calib. Spinning, CPU
	 * = duration; sleeping, CPU = the reads alone. The counters have been on
	 * since week 4: CONFIG_THREAD_ANALYZER selects them. */
	k_thread_runtime_stats_t u0, u1;

	k_thread_runtime_stats_get(k_current_get(), &u0);
#endif

	printk("calibrating zero-flow offset (%d rounds)...\n", CALIB_ROUNDS);
	for (int i = 0; i < CALIB_ROUNDS; i++) {
		(void)read_pressure_mv();
#ifdef CONFIG_LAB_ADR005
		/* ADR-005: wait for the sensor instead of spinning. The sleep
		 * rounds up to the kernel tick: 1 ms on the C6 by default. */
		k_usleep(400);
#else
		k_busy_wait(400); /* sensor settling time between reads */
#endif
	}
#ifdef CONFIG_SCHED_THREAD_USAGE
	k_thread_runtime_stats_get(k_current_get(), &u1);
	printk("calibration done (%u ms, CPU %u us)\n", k_uptime_get_32() - t0,
	       (unsigned int)k_cyc_to_us_floor64(u1.execution_cycles -
						 u0.execution_cycles));
#else
	printk("calibration done (%u ms)\n", k_uptime_get_32() - t0);
#endif
}

extern const k_tid_t telemetry_tid;

/* Week 6 Task B: the classic sequence, INV_ROUNDS times. The console (low)
 * takes the PI lock just before a control release, control (high) asks for it
 * and waits, and the console then wakes telemetry (medium). With a k_sem
 * telemetry preempts the console and control waits for both; with a k_mutex
 * the console runs at control's priority until it unlocks. */
static void cmd_invert(void)
{
	const uint32_t cyc_per_us = sys_clock_hw_cycles_per_sec() / 1000000;
	atomic_val_t missed0 = atomic_get(&ctrl_missed);
	int done = 0;

	atomic_set(&ctrl_r_peak_us, 0);
	atomic_set(&cs_peak_us, 0);

	for (int tries = 0; done < INV_ROUNDS && tries < 10 * INV_ROUNDS; tries++) {
		/* Spin, preemptibly, to 9.2-9.6 ms after control's latest release:
		 * the next one lands 0.4-0.8 ms into the section. The busy wait is
		 * for native_sim, whose clock only moves inside one. */
		for (;;) {
			uint32_t since = (k_cycle_get_32() -
					  (uint32_t)atomic_get(&ctrl_released)) / cyc_per_us;

			if (since >= 9200 && since < 9600) {
				break;
			}
			k_busy_wait(10);
		}

		enum lock_kind k = pi_lock();
		uint32_t t0 = k_cycle_get_32();
		bool kicked = false;

		instr_set(&instr_lock, 1);
		/* The section's work in 10 us slices, so the console notices
		 * control asking. A slice cut by preemption ends early by at most
		 * 10 us, so the work stays INV_CS_US whoever runs in between. */
		for (int i = 0; i < INV_CS_US / 10; i++) {
			k_busy_wait(10);
			if (!kicked && atomic_get(&ctrl_waiting)) {
				kicked = true;
				atomic_set(&medium_kick, 1);
				k_wakeup(telemetry_tid);
			}
		}
		instr_set(&instr_lock, 0);

		atomic_val_t cs_us = (k_cycle_get_32() - t0) / cyc_per_us;

		pi_unlock(k);

		atomic_clear(&medium_kick); /* unused if telemetry was printing */
		if (cs_us > atomic_get(&cs_peak_us)) {
			atomic_set(&cs_peak_us, cs_us);
		}
		if (kicked) {
			done++; /* the release missed the section otherwise: retry */
		}
		k_msleep(100);
	}
	printk("invert %s: %d rounds, control R max %ld us, section max %ld us, "
	       "ctrl_missed +%ld\n", lock_kind == LOCK_SEM ? "sem" : "mutex", done,
	       (long)atomic_get(&ctrl_r_peak_us), (long)atomic_get(&cs_peak_us),
	       (long)(atomic_get(&ctrl_missed) - missed0));
}

static void console_handle(char *line)
{
	if (strcmp(line, "help") == 0) {
		printk("commands: help status joy set <mv> clear calib threads "
		       "lock sem|mutex invert\n");
	} else if (strcmp(line, "status") == 0) {
		printk("p=%d mV sp=%d mV duty=%d%% flow_x100=%d estop=%d "
		       "batches=%u backlog_peak=%ld lat_peak_us=%ld ticks_dropped=%ld "
		       "ctrl_missed=%ld ctrl_r_peak_us=%ld lock=%s\n",
		       pressure_mv, setpoint_mv, duty_pct,
		       (int)(flow_lpm * 100.0f), (int)estop, flow_batches,
		       (long)atomic_get(&backlog_peak),
		       (long)atomic_get(&lat_peak_us), (long)atomic_get(&ticks_dropped),
		       (long)atomic_get(&ctrl_missed),
		       (long)atomic_get(&ctrl_r_peak_us),
		       lock_kind == LOCK_SEM ? "sem" : "mutex");
	} else if (strcmp(line, "joy") == 0) {
		printk("joystick raw: %d mV\n", joystick_mv);
	} else if (strncmp(line, "set ", 4) == 0) {
		enum lock_kind k = pi_lock();

		setpoint_mv = CLAMP(atoi(line + 4), 0, 3300);
		pi_unlock(k);
		printk("setpoint = %d mV\n", setpoint_mv);
	} else if (strcmp(line, "clear") == 0) {
		enum lock_kind k = pi_lock();

		estop = false;
		integral_mv = 0;
		pi_unlock(k);
		printk("e-stop cleared\n");
	} else if (strcmp(line, "calib") == 0) {
		cmd_calib();
	} else if (strcmp(line, "lock sem") == 0 || strcmp(line, "lock mutex") == 0) {
		lock_kind = line[5] == 's' ? LOCK_SEM : LOCK_MUTEX;
		atomic_set(&ctrl_r_peak_us, 0);
		printk("PI lock = %s, ctrl_r_peak_us reset\n", line + 5);
	} else if (strcmp(line, "invert") == 0) {
		cmd_invert();
	} else if (strcmp(line, "threads") == 0) {
#ifdef CONFIG_THREAD_ANALYZER
		/* Week 4 Task D: stack high-water marks. The node has no shell (the
		 * console owns the UART), so the analyzer hangs off a command. */
		thread_analyzer_print(0);
#else
		printk("thread analyzer not built in (CONFIG_THREAD_ANALYZER=n)\n");
#endif
	} else if (line[0] != '\0') {
		printk("? (try: help)\n");
	}
}

static void task_console(void)
{
	static char buf[32];
	static size_t len;
	unsigned char c;

#if DT_HAS_CHOSEN(zephyr_console)
	while (uart_poll_in(console_uart, &c) == 0) {
		instr_set(&instr_cons, 1);
		if (c == '\r' || c == '\n') {
			buf[len] = '\0';
			printk("\n");
			console_handle(buf);
			len = 0;
		} else if (len < sizeof(buf) - 1) {
			buf[len++] = c;
			printk("%c", c);
		}
		instr_set(&instr_cons, 0);
	}
#endif
}

/* ---- Threads and deferred work -------------------------------------------- */

/* Every thread blocks. One that polled without sleeping would own every cycle
 * below its priority, idle included, and week 5's CPU-load reading with it. */

static void sampling_thread(void *p1, void *p2, void *p3)
{
	uint32_t released;
	int control_div = 0;

	while (1) {
		k_msgq_get(&tick_q, &released, K_FOREVER);

		atomic_val_t lat = k_cyc_to_us_floor32(k_cycle_get_32() - released);

		if (lat > atomic_get(&lat_peak_us)) {
			atomic_set(&lat_peak_us, lat);
		}

		task_sampling();
		if (++control_div >= CONTROL_EVERY) {
			control_div = 0;
			if (k_sem_count_get(&control_sem) > 0) {
				atomic_inc(&ctrl_missed);
			}
			atomic_set(&ctrl_released, released);
			/* Task C measures from instr_samp falling to instr_ctrl
			 * rising: this give, the block on tick_q below, and the
			 * switch to control in between. */
			k_sem_give(&control_sem);
		}
	}
}
K_THREAD_DEFINE(sampling_tid, SAMPLING_STACK, sampling_thread, NULL, NULL, NULL,
		SAMPLING_PRIO, 0, 0);

static void control_thread(void *p1, void *p2, void *p3)
{
	while (1) {
		k_sem_take(&control_sem, K_FOREVER);

		/* Read at the take, before any wait for the PI lock: the next
		 * release would overwrite it. */
		uint32_t released = atomic_get(&ctrl_released);

		task_control();

		/* Week 6: response time from the release tick, for the RTA */
		atomic_val_t r = k_cyc_to_us_floor32(k_cycle_get_32() - released);

		if (r > atomic_get(&ctrl_r_peak_us)) {
			atomic_set(&ctrl_r_peak_us, r);
		}
	}
}
K_THREAD_DEFINE(control_tid, CONTROL_STACK, control_thread, NULL, NULL, NULL,
		CONTROL_PRIO, 0, 0);

static void flow_work_fn(struct k_work *work)
{
	task_flow_batch();
}

static void display_thread(void *p1, void *p2, void *p3)
{
	while (1) {
		task_display();
		k_msleep(DISPLAY_POLL_MS);
	}
}
K_THREAD_DEFINE(display_tid, DISPLAY_STACK, display_thread, NULL, NULL, NULL,
		DISPLAY_PRIO, 0, 0);

static void telemetry_thread(void *p1, void *p2, void *p3)
{
	/* Absolute deadlines: next += 1000 does not drift with the time the
	 * line itself takes, which a relative k_msleep(1000) would. */
	int64_t next_ms = k_uptime_get() + TELEMETRY_MS;

	while (1) {
		k_sleep(K_TIMEOUT_ABS_MS(next_ms));
		/* Woken early by `invert` (k_wakeup): busy work, same 1 s grid. */
		if (atomic_cas(&medium_kick, 1, 0)) {
			task_medium();
			continue;
		}
		task_telemetry();
		next_ms += TELEMETRY_MS;
	}
}
K_THREAD_DEFINE(telemetry_tid, TELEMETRY_STACK, telemetry_thread, NULL, NULL,
		NULL, TELEMETRY_PRIO, 0, 0);

static void console_thread(void *p1, void *p2, void *p3)
{
	while (1) {
		task_console();
		k_msleep(CONSOLE_POLL_MS);
	}
}
K_THREAD_DEFINE(console_tid, CONSOLE_STACK, console_thread, NULL, NULL, NULL,
		CONSOLE_PRIO, 0, 0);

/* ---- Bring-up ------------------------------------------------------------- */

static void init_hw(void)
{
	const struct gpio_dt_spec *outs[] = { &instr_samp, &instr_ctrl,
					      &instr_cons, &instr_tele,
					      &instr_flow, &instr_disp,
					      &instr_lock, &valve_led };

	for (size_t i = 0; i < ARRAY_SIZE(outs); i++) {
		if (outs[i]->port) {
			gpio_pin_configure_dt(outs[i], GPIO_OUTPUT_INACTIVE);
		}
	}
#if HAS_ADC
	adc_channel_setup_dt(&adc_joy);
	adc_channel_setup_dt(&adc_pressure);
#endif
	if (flow_pulse.port) {
		gpio_pin_configure_dt(&flow_pulse, GPIO_INPUT);
		gpio_pin_interrupt_configure_dt(&flow_pulse,
						GPIO_INT_EDGE_TO_ACTIVE);
		gpio_init_callback(&flow_cb, flow_isr, BIT(flow_pulse.pin));
		gpio_add_callback(flow_pulse.port, &flow_cb);
	}
}

int main(void)
{
	const struct k_work_queue_config flow_wq_cfg = { .name = "flow_wq" };

	/* Before init_hw(): the flow ISR may submit as soon as it is armed. */
	k_work_queue_start(&flow_wq, flow_wq_stack,
			   K_THREAD_STACK_SIZEOF(flow_wq_stack), FLOW_WQ_PRIO,
			   &flow_wq_cfg);
	k_work_init(&flow_work, flow_work_fn);

	init_hw();
	printk("SoilSense Control: kernel build (%s)\n", CONFIG_BOARD);
	printk("type 'help'\n");

	k_timer_start(&tick_timer, K_USEC(SAMPLE_PERIOD_US),
		      K_USEC(SAMPLE_PERIOD_US));
	return 0;
}
