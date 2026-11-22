# Build the C tracker. On the laptop for testing (any C99 compiler), or for QNX with the SDK's compiler:
#   qnxsdp-env.bat            (sets up QNX_HOST / QNX_TARGET / PATH)
#   make CC="qcc -Vgcc_ntoaarch64le" TARGET=tracker_demo
CC ?= cc
CFLAGS ?= -O2 -std=c99 -Wall -Wextra

libturret_tracker.so: tracker.c tracker.h
	$(CC) $(CFLAGS) -shared -fPIC -o $@ tracker.c -lm

clean:
	rm -f libturret_tracker.so *.o

turretd: turretd.c tracker.c engage.c tracker.h engage.h
	$(CC) -O2 -std=gnu99 -Wall -Wextra -o $@ turretd.c tracker.c engage.c -lm -lsocket
