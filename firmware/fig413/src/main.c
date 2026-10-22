/*
 * Week 6, Task A: Buttazzo's Fig. 4.13, live on the node's board.
 *
 *   tau1  C = 2 ms, T = D = 5 ms
 *   tau2  C = 4 ms, T = D = 7 ms        U = 2/5 + 4/7 = 0.971
 *
 * Default build: rate-monotonic priorities, tau1 above tau2. From the shared
 * release at t = 0, tau1 preempts tau2 at t = 5 with one unit left and tau2
 * misses its deadline at t = 7. A late job keeps running and the next release
 * stays on the absolute grid, so the set recovers inside the 35 ms hyperperiod
 * and misses again in the next one: one miss per 35 ms, 1715 in 60 s.
 *
 * With edf.conf (CONFIG_SCHED_DEADLINE=y) both threads share one static
 * priority and the kernel picks the earlier absolute deadline. U <= 1, so
 * nothing misses.
 *
 * Instrumentation, one pin per task plus one per deadline:
 *   tau1_run / tau2_run    high from the job's start to its end, and still
 *                          high while the job is preempted
 *   tau1_miss / tau2_miss  high from the missed deadline to the late job's end
 * The console prints jobs, misses, worst response and worst lateness every 5 s.
 */

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/printk.h>

#define START_DELAY_MS 500 /* lets the console banner drain before t = 0 */
#define REPORT_MS      5000

#ifdef CONFIG_SCHED_DEADLINE
#define TAU1_PRIO 1 /* EDF orders threads within one priority by deadline */
#define TAU2_PRIO 1
#define POLICY    "EDF"
#else
#define TAU1_PRIO 1 /* RM: the shorter period gets the higher priority */
#define TAU2_PRIO 2
#define POLICY    "RM"
#endif
#define REPORT_PRIO 14 /* below both: it only runs in the 2.9 % they leave */

#define PIN(node) GPIO_DT_SPEC_GET_OR(DT_NODELABEL(node), gpios, {0})

static inline void pin_set(const struct gpio_dt_spec *p, int v)
{
	if (p->port) {
		gpio_pin_set_dt(p, v);
	}
}

/* ---- Computation: a busy-wait that counts work, not time ------------------ */

/* k_busy_wait() spins on the clock: a job preempted halfway would "finish" on
 * time with half its work undone. A loop of N iterations only advances while
 * the thread holds the CPU, which is what C means. N is calibrated at boot. */
static uint32_t loops_per_ms;

static void spin_loops(uint32_t n)
{
	for (volatile uint32_t i = 0; i < n; i++) {
	}
}

static void spin_ms(uint32_t ms)
{
	spin_loops(ms * loops_per_ms);
}

static void calibrate(void)
{
	const uint32_t probe = 200000;

	/* Interrupts locked so nothing stretches the reference run; the first
	 * pass only warms the cache. */
	for (int pass = 0; pass < 2; pass++) {
		unsigned int key = irq_lock();
		uint32_t t0 = k_cycle_get_32();

		spin_loops(probe);
		uint32_t dt = k_cycle_get_32() - t0;

		irq_unlock(key);
		loops_per_ms = (uint64_t)probe * sys_clock_hw_cycles_per_sec() /
			       (1000ULL * dt);
	}
}

/* ---- The task set --------------------------------------------------------- */

struct rt_task {
	const char *name;
	uint32_t c_ms;
	uint32_t t_ms; /* D = T */
	int prio;
	struct gpio_dt_spec run;
	struct gpio_dt_spec miss;
	struct k_timer dl_timer;
	atomic_t busy; /* job released and not finished: read by dl_timer */

	/* written by the task only, read by the reporter */
	uint32_t jobs;
	uint32_t misses;
	uint32_t r_max_us;    /* release -> end */
	uint32_t late_max_us; /* deadline -> end, misses only */
	uint32_t wake_max_us; /* release -> start, under 1 ms: the kernel's wake-up
			       * cost for tau1 under RM, plus the other job's tail
			       * otherwise */
};

static struct rt_task tasks[] = {
	{ .name = "tau1", .c_ms = 2, .t_ms = 5, .prio = TAU1_PRIO,
	  .run = PIN(tau1_run), .miss = PIN(tau1_miss) },
	{ .name = "tau2", .c_ms = 4, .t_ms = 7, .prio = TAU2_PRIO,
	  .run = PIN(tau2_run), .miss = PIN(tau2_miss) },
};

static int64_t start_ms; /* common first release: the critical instant */

/* Fires at each deadline. If the job is still running, it just missed. */
static void dl_expired(struct k_timer *timer)
{
	struct rt_task *t = CONTAINER_OF(timer, struct rt_task, dl_timer);

	if (atomic_get(&t->busy)) {
		pin_set(&t->miss, 1);
	}
}

static uint32_t cyc_to_us(uint64_t cyc)
{
	return (uint32_t)k_cyc_to_us_floor64(cyc);
}

static void task_thread(void *p1, void *p2, void *p3)
{
	struct rt_task *t = p1;
	int64_t release = start_ms;

	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	for (;;) {
		int64_t deadline = release + t->t_ms;
		/* The kernel keeps cycles = ticks * cycles-per-tick, so a time in ms
		 * maps exactly onto k_cycle_get_64(). */
		uint64_t rel_cyc = k_ms_to_cyc_floor64(release);
		uint64_t dl_cyc = k_ms_to_cyc_floor64(deadline);

#ifdef CONFIG_SCHED_DEADLINE
		/* Before blocking, not after waking: the wake-up already competes
		 * with whatever deadline the thread holds, and setting the new one
		 * later does not reschedule. The run would look like RM. */
		k_thread_absolute_deadline_set(k_current_get(), (int)(uint32_t)dl_cyc);
#endif
		/* A late job must start at once. k_sleep() to an absolute time
		 * already past does not return at once: it still waits for the next
		 * tick, up to 1 ms of idle CPU that makes the following job late as
		 * well. Measured: without this check tau2 missed 717 of 717. */
		if (k_cycle_get_64() < rel_cyc) {
			k_sleep(K_TIMEOUT_ABS_MS(release));
		}

		/* Armed after the wake-up: a job still finishing the previous one
		 * gets here late, and its own deadline only counts once released. */
		atomic_set(&t->busy, 1);
		k_timer_start(&t->dl_timer, K_TIMEOUT_ABS_MS(deadline), K_NO_WAIT);

		uint64_t begin = k_cycle_get_64();

		pin_set(&t->run, 1);
		spin_ms(t->c_ms);
		pin_set(&t->run, 0);

		uint64_t end = k_cycle_get_64();

		atomic_set(&t->busy, 0);
		pin_set(&t->miss, 0);

		t->jobs++;
		t->r_max_us = MAX(t->r_max_us, cyc_to_us(end - rel_cyc));
		if (end > dl_cyc) {
			t->misses++;
			t->late_max_us = MAX(t->late_max_us, cyc_to_us(end - dl_cyc));
		}
		if (begin >= rel_cyc && begin - rel_cyc < k_ms_to_cyc_floor64(1)) {
			t->wake_max_us = MAX(t->wake_max_us, cyc_to_us(begin - rel_cyc));
		}

		release += t->t_ms;
	}
}

K_THREAD_STACK_ARRAY_DEFINE(task_stacks, ARRAY_SIZE(tasks), 1024);
static struct k_thread task_threads[ARRAY_SIZE(tasks)];

int main(void)
{
	for (size_t i = 0; i < ARRAY_SIZE(tasks); i++) {
		struct rt_task *t = &tasks[i];

		if (t->run.port) {
			gpio_pin_configure_dt(&t->run, GPIO_OUTPUT_INACTIVE);
		}
		if (t->miss.port) {
			gpio_pin_configure_dt(&t->miss, GPIO_OUTPUT_INACTIVE);
		}
		k_timer_init(&t->dl_timer, dl_expired, NULL);
	}

	calibrate();

	/* Check the calibration against the clock, uncontended. */
	uint32_t t0 = k_cycle_get_32();

	spin_ms(2);
	uint32_t spin2_us = cyc_to_us(k_cycle_get_32() - t0);

	printk("\nfig413 %s  tau1 C=2 T=5  tau2 C=4 T=7  U=0.971\n", POLICY);
	printk("loops_per_ms=%u  spin_ms(2)=%u us\n", loops_per_ms, spin2_us);

	k_thread_priority_set(k_current_get(), REPORT_PRIO);

	/* Rounded to a whole ms so every release lands on a tick. */
	start_ms = k_uptime_get() + START_DELAY_MS;
	for (size_t i = 0; i < ARRAY_SIZE(tasks); i++) {
		struct rt_task *t = &tasks[i];
		k_tid_t tid = k_thread_create(&task_threads[i], task_stacks[i],
					      K_THREAD_STACK_SIZEOF(task_stacks[i]),
					      task_thread, t, NULL, NULL,
					      t->prio, 0, K_NO_WAIT);
		k_thread_name_set(tid, t->name);
	}

	for (int64_t next = start_ms + REPORT_MS;; next += REPORT_MS) {
		k_sleep(K_TIMEOUT_ABS_MS(next));
		printk("t=%2d s", (int)((next - start_ms) / 1000));
		for (size_t i = 0; i < ARRAY_SIZE(tasks); i++) {
			struct rt_task *t = &tasks[i];

			printk("  %s jobs=%u miss=%u Rmax=%u us late_max=%u us wake_max=%u us",
			       t->name, t->jobs, t->misses, t->r_max_us,
			       t->late_max_us, t->wake_max_us);
		}
		printk("\n");
	}
	return 0;
}
