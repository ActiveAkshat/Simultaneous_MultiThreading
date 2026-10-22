CC ?= gcc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra -D_GNU_SOURCE

main: main.c jobs.h libjobs.a
	$(CC) $(CFLAGS) main.c libjobs.a -o main -pthread

clean:
	rm -f main

.PHONY: clean