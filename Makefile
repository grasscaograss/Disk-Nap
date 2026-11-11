PYTHON ?= python3
CC = clang

.PHONY: all test

all: build/usb-control

build/usb-control: native/usb-control.c
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) -std=c17 -O2 -Wall -Wextra -Werror $< \
		$(LDFLAGS) -framework IOKit -framework CoreFoundation -o $@

test: all
	$(PYTHON) -m unittest discover -s tests -v
