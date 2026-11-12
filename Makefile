PYTHON ?= python3
CC = clang
MENU_APP = build/Disk Nap.app
APP_VERSION = $(shell /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' menubar/Info.plist)
RELEASE_ARCHIVE = build/Disk-Nap-v$(APP_VERSION)-macOS-$(shell uname -m).zip

.PHONY: all test menubar package

all: build/usb-control build/power-events

build/usb-control: native/usb-control.c
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) -std=c17 -O2 -Wall -Wextra -Werror $< \
		$(LDFLAGS) -framework IOKit -framework CoreFoundation -o $@

build/power-events: native/power-events.c Makefile
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) -mmacosx-version-min=14.0 -std=c17 -O2 -Wall -Wextra -Werror $< \
		$(LDFLAGS) -framework IOKit -framework CoreFoundation -o $@

test: all
	$(PYTHON) -m unittest discover -s tests -v

menubar: build/power-events
	$(RM) -r "$(MENU_APP)"
	mkdir -p "$(MENU_APP)/Contents/MacOS" "$(MENU_APP)/Contents/Resources/disk_nap" "$(MENU_APP)/Contents/Resources/build"
	cp menubar/Info.plist "$(MENU_APP)/Contents/Info.plist"
	cp -R menubar/*.lproj "$(MENU_APP)/Contents/Resources/"
	cp disk_nap/*.py "$(MENU_APP)/Contents/Resources/disk_nap/"
	cp build/power-events "$(MENU_APP)/Contents/Resources/build/"
	@if [ -n "$(LAB_PYTHON)" ]; then plutil -insert LabPythonExecutable -string "$(LAB_PYTHON)" "$(MENU_APP)/Contents/Info.plist"; fi
	xcrun swiftc -target "$$(uname -m)-apple-macosx14.0" -swift-version 5 -O -parse-as-library menubar/*.swift -o "$(MENU_APP)/Contents/MacOS/DiskNap"
	codesign --force --sign - "$(MENU_APP)"

package: override LAB_PYTHON :=
package: menubar
	codesign --verify --deep --strict "$(MENU_APP)"
	/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$(MENU_APP)" "$(RELEASE_ARCHIVE)"
	cd build && /usr/bin/shasum -a 256 "$(notdir $(RELEASE_ARCHIVE))" > "$(notdir $(RELEASE_ARCHIVE)).sha256"
