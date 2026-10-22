#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <linux/perf_event.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "jobs.h"

#define DEFAULT_WARMUP_ITERS 50
#define DEFAULT_MEAS_ITERS   300

typedef struct {
  int job_id;
  int cpu_id;
  int warmup_iters;
  int meas_iters;
  pthread_barrier_t *barrier;
  atomic_int *stop_flag;
  int is_paired;
  int ctl_fd;
  int ack_fd;

  uint64_t instructions;
  uint64_t cycles;
  uint64_t cache_refs;
  uint64_t cache_misses;
  double ipc;
  int actual_iters;
} thread_arg_t;

static long perf_event_open(struct perf_event_attr *hw_event, pid_t pid,
                            int cpu, int group_fd, unsigned long flags) {
  return syscall(__NR_perf_event_open, hw_event, pid, cpu, group_fd, flags);
}

static int open_counter(uint32_t type, uint64_t config, int group_fd) {
  struct perf_event_attr pe;
  memset(&pe, 0, sizeof(struct perf_event_attr));
  pe.type = type;
  pe.size = sizeof(struct perf_event_attr);
  pe.config = config;
  pe.disabled = 1;
  pe.exclude_kernel = 1;
  pe.exclude_hv = 1;
  return (int)perf_event_open(&pe, 0, -1, group_fd, 0);
}

static void perf_ctl_notify(int ctl_fd, int ack_fd, const char *cmd) {
  if (ctl_fd >= 0) {
    ssize_t w = write(ctl_fd, cmd, strlen(cmd));
    (void)w;
    if (ack_fd >= 0) {
      char buf[16];
      ssize_t r = read(ack_fd, buf, sizeof(buf));
      (void)r;
    }
  }
}

static void *worker_thread(void *arg) {
  thread_arg_t *t = (thread_arg_t *)arg;

  // Pin thread to its designated SMT sibling logical CPU
  cpu_set_t cpuset;
  CPU_ZERO(&cpuset);
  CPU_SET(t->cpu_id, &cpuset);
  if (pthread_setaffinity_np(pthread_self(), sizeof(cpu_set_t), &cpuset) != 0) {
    perror("pthread_setaffinity_np");
  }

  // Open per-thread hardware performance counters
  int fd_inst = open_counter(PERF_TYPE_HARDWARE, PERF_COUNT_HW_INSTRUCTIONS, -1);
  int fd_cycl = open_counter(PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES, fd_inst);
  int fd_cref = open_counter(PERF_TYPE_HARDWARE, PERF_COUNT_HW_CACHE_REFERENCES, fd_inst);
  int fd_cmis = open_counter(PERF_TYPE_HARDWARE, PERF_COUNT_HW_CACHE_MISSES, fd_inst);

  Job fn = jobs[t->job_id];

  // Warm-up phase
  for (int i = 0; i < t->warmup_iters; i++) {
    fn();
  }

  // Synchronize threads right before timed measurement
  pthread_barrier_wait(t->barrier);

  if (t->ctl_fd >= 0) {
    perf_ctl_notify(t->ctl_fd, t->ack_fd, "enable\n");
  }

  if (fd_inst >= 0) ioctl(fd_inst, PERF_EVENT_IOC_RESET, PERF_IOC_FLAG_GROUP);
  if (fd_inst >= 0) ioctl(fd_inst, PERF_EVENT_IOC_ENABLE, PERF_IOC_FLAG_GROUP);

  int iters = 0;
  if (!t->is_paired) {
    for (int i = 0; i < t->meas_iters; i++) {
      fn();
    }
    iters = t->meas_iters;
    if (fd_inst >= 0) ioctl(fd_inst, PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP);
  } else {
    for (int i = 0; i < t->meas_iters; i++) {
      fn();
    }
    iters = t->meas_iters;

    // Disable this thread's counters immediately after finishing its measurement window
    if (fd_inst >= 0) ioctl(fd_inst, PERF_EVENT_IOC_DISABLE, PERF_IOC_FLAG_GROUP);

    // Keep generating the exact same SMT contention for the sibling thread
    // until the sibling thread also finishes its measured iterations
    int prev = atomic_fetch_add(t->stop_flag, 1);
    if (prev == 0) {
      while (atomic_load(t->stop_flag) < 2) {
        fn();
      }
    }
  }

  if (t->ctl_fd >= 0) {
    perf_ctl_notify(t->ctl_fd, t->ack_fd, "disable\n");
  }

  uint64_t inst = 0, cycl = 0, cref = 0, cmis = 0;
  if (fd_inst >= 0) {
    ssize_t r;
    r = read(fd_inst, &inst, sizeof(uint64_t)); (void)r;
    if (fd_cycl >= 0) { r = read(fd_cycl, &cycl, sizeof(uint64_t)); (void)r; }
    if (fd_cref >= 0) { r = read(fd_cref, &cref, sizeof(uint64_t)); (void)r; }
    if (fd_cmis >= 0) { r = read(fd_cmis, &cmis, sizeof(uint64_t)); (void)r; }
  }

  if (fd_inst >= 0) close(fd_inst);
  if (fd_cycl >= 0) close(fd_cycl);
  if (fd_cref >= 0) close(fd_cref);
  if (fd_cmis >= 0) close(fd_cmis);

  t->instructions = inst;
  t->cycles = cycl;
  t->cache_refs = cref;
  t->cache_misses = cmis;
  t->ipc = (cycl > 0) ? ((double)inst / (double)cycl) : 0.0;
  t->actual_iters = iters;

  return NULL;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr,
            "usage:\n"
            "  %s <serial> [solo <job> <cpu> [warmup] [iters] [ctl_fd] [ack_fd]]\n"
            "  %s <serial> [pair <jobA> <jobB> <cpuA> <cpuB> [warmup] [iters]]\n",
            argv[0], argv[0]);
    return 1;
  }

  uint32_t serial = (uint32_t)strtoul(argv[1], NULL, 10);
  jobs_init(serial);

  if (argc == 2) {
    pthread_barrier_t barrier;
    pthread_barrier_init(&barrier, NULL, 1);
    atomic_int stop_flag = 0;
    for (int j = 0; j < NUM_JOBS; j++) {
      thread_arg_t targ = {
          .job_id = j,
          .cpu_id = 0,
          .warmup_iters = DEFAULT_WARMUP_ITERS,
          .meas_iters = DEFAULT_MEAS_ITERS,
          .barrier = &barrier,
          .stop_flag = &stop_flag,
          .is_paired = 0,
          .ctl_fd = -1,
          .ack_fd = -1,
      };
      pthread_t tid;
      pthread_create(&tid, NULL, worker_thread, &targ);
      pthread_join(tid, NULL);
      printf("SOLO job=%d ipc=%.6f inst=%" PRIu64 " cycles=%" PRIu64 " cref=%" PRIu64 " cmis=%" PRIu64 "\n",
             j, targ.ipc, targ.instructions, targ.cycles,
             targ.cache_refs, targ.cache_misses);
    }
    pthread_barrier_destroy(&barrier);
    return 0;
  }

  const char *mode = argv[2];

  if (strcmp(mode, "solo") == 0) {
    if (argc < 5) {
      fprintf(stderr, "usage: %s <serial> solo <job> <cpu> [warmup] [iters] [ctl_fd] [ack_fd]\n", argv[0]);
      return 1;
    }
    int job_id = atoi(argv[3]);
    int cpu_id = atoi(argv[4]);
    int warmup = (argc >= 6) ? atoi(argv[5]) : DEFAULT_WARMUP_ITERS;
    int iters  = (argc >= 7) ? atoi(argv[6]) : DEFAULT_MEAS_ITERS;
    int ctl_fd = (argc >= 8) ? atoi(argv[7]) : -1;
    int ack_fd = (argc >= 9) ? atoi(argv[8]) : -1;

    pthread_barrier_t barrier;
    pthread_barrier_init(&barrier, NULL, 1);
    atomic_int stop_flag = 0;

    thread_arg_t targ = {
        .job_id = job_id,
        .cpu_id = cpu_id,
        .warmup_iters = warmup,
        .meas_iters = iters,
        .barrier = &barrier,
        .stop_flag = &stop_flag,
        .is_paired = 0,
        .ctl_fd = ctl_fd,
        .ack_fd = ack_fd,
    };

    pthread_t tid;
    pthread_create(&tid, NULL, worker_thread, &targ);
    pthread_join(tid, NULL);
    pthread_barrier_destroy(&barrier);

    printf("SOLO job=%d ipc=%.6f inst=%" PRIu64 " cycles=%" PRIu64 " cref=%" PRIu64 " cmis=%" PRIu64 "\n",
           job_id, targ.ipc, targ.instructions, targ.cycles,
           targ.cache_refs, targ.cache_misses);
    return 0;

  } else if (strcmp(mode, "pair") == 0) {
    if (argc < 7) {
      fprintf(stderr, "usage: %s <serial> pair <jobA> <jobB> <cpuA> <cpuB> [warmup] [iters]\n", argv[0]);
      return 1;
    }
    int jobA = atoi(argv[3]);
    int jobB = atoi(argv[4]);
    int cpuA = atoi(argv[5]);
    int cpuB = atoi(argv[6]);
    int warmup = (argc >= 8) ? atoi(argv[7]) : DEFAULT_WARMUP_ITERS;
    int iters  = (argc >= 9) ? atoi(argv[8]) : DEFAULT_MEAS_ITERS;

    pthread_barrier_t barrier;
    pthread_barrier_init(&barrier, NULL, 2);
    atomic_int stop_flag = 0;

    thread_arg_t targA = {
        .job_id = jobA,
        .cpu_id = cpuA,
        .warmup_iters = warmup,
        .meas_iters = iters,
        .barrier = &barrier,
        .stop_flag = &stop_flag,
        .is_paired = 1,
        .ctl_fd = -1,
        .ack_fd = -1,
    };

    thread_arg_t targB = {
        .job_id = jobB,
        .cpu_id = cpuB,
        .warmup_iters = warmup,
        .meas_iters = iters,
        .barrier = &barrier,
        .stop_flag = &stop_flag,
        .is_paired = 1,
        .ctl_fd = -1,
        .ack_fd = -1,
    };

    pthread_t tidA, tidB;
    pthread_create(&tidA, NULL, worker_thread, &targA);
    pthread_create(&tidB, NULL, worker_thread, &targB);
    pthread_join(tidA, NULL);
    pthread_join(tidB, NULL);
    pthread_barrier_destroy(&barrier);

    printf("PAIR jobA=%d jobB=%d ipcA=%.6f ipcB=%.6f instA=%" PRIu64 " cyclesA=%" PRIu64 " instB=%" PRIu64 " cyclesB=%" PRIu64 "\n",
           jobA, jobB, targA.ipc, targB.ipc,
           targA.instructions, targA.cycles,
           targB.instructions, targB.cycles);
    return 0;
  }

  fprintf(stderr, "Unknown mode: %s\n", mode);
  return 1;
}