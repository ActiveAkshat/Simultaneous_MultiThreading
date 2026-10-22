
#ifndef JOBS_H
#define JOBS_H

#include <assert.h>
#include <stdint.h>

#define NUM_JOBS 10

_Static_assert(NUM_JOBS % 2 == 0,
               "Number of jobs must be even");

typedef void (*Job)(void);

/* Job function pointers assigned by jobs_init(). */
extern Job jobs[NUM_JOBS];

/* Call once before accessing jobs[]. */
void jobs_init(uint32_t serial_number);

#endif
