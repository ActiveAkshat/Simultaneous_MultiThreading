// C-Standard: C11 or newer

#pragma once

#include <assert.h>
#include <stdint.h>

#define NUM_JOBS 10

static_assert(NUM_JOBS % 2 == 0, "Number of jobs must be even");

typedef void (*Job)(void);

/* Holds job function pointers assigned by jobs_init(). */
extern Job jobs[NUM_JOBS];

/* Call this once before running or indexing into jobs. */
void jobs_init(uint32_t serial_number);
