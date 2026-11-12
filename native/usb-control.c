#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/storage/IOMedia.h>
#include <IOKit/usb/IOUSBLib.h>

#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

static volatile sig_atomic_t wake_requested;

static void request_wake(int signal_number) {
    (void)signal_number;
    wake_requested = 1;
}

static IOReturn report(const char *operation, IOReturn result) {
    printf("%s=0x%08x\n", operation, (unsigned)result);
    fflush(stdout);
    return result;
}

static bool parse_number(const char *text, unsigned long long *number) {
    if (!*text || strspn(text, "0123456789") != strlen(text)) {
        return false;
    }
    errno = 0;
    *number = strtoull(text, NULL, 10);
    return !errno;
}

static bool is_unmounted(const char *whole_disk, const char *mount_point) {
    struct statfs *mounts = NULL;
    int count = getmntinfo(&mounts, MNT_NOWAIT);
    if (count <= 0) {
        return false;
    }
    size_t length = strlen(whole_disk);
    for (int i = 0; i < count; i++) {
        const char *source = mounts[i].f_mntfromname;
        if ((!strncmp(source, whole_disk, length) &&
             (source[length] == '\0' || source[length] == 's')) ||
            !strcmp(mounts[i].f_mntonname, mount_point)) {
            return false;
        }
    }
    return true;
}

static bool has_no_open_media(uint64_t entry_id) {
    io_service_t service = IOServiceGetMatchingService(
        kIOMainPortDefault, IORegistryEntryIDMatching(entry_id));
    if (!service) {
        return false;
    }
    io_iterator_t children = IO_OBJECT_NULL;
    kern_return_t result = IORegistryEntryCreateIterator(
        service, kIOServicePlane, kIORegistryIterateRecursively, &children);
    IOObjectRelease(service);
    if (result != KERN_SUCCESS || !children) {
        return false;
    }
    // Disk Arbitration can eject an APFS disk while its closed media stay registered.
    bool safe = false;
    for (int attempt = 0; attempt < 3 && !safe; attempt++) {
        io_registry_entry_t child;
        bool open_or_unknown = false;
        while ((child = IOIteratorNext(children))) {
            if (IOObjectConformsTo(child, kIOMediaClass)) {
                CFTypeRef open = IORegistryEntryCreateCFProperty(
                    child, CFSTR(kIOMediaOpenKey), kCFAllocatorDefault, 0);
                open_or_unknown = open != kCFBooleanFalse;
                if (open) {
                    CFRelease(open);
                }
            }
            IOObjectRelease(child);
            if (open_or_unknown) {
                break;
            }
        }
        if (open_or_unknown) {
            break;
        }
        safe = IOIteratorIsValid(children);
        if (!safe) {
            IOIteratorReset(children);
        }
    }
    IOObjectRelease(children);
    return safe;
}

static bool matches_serial(io_service_t service, const char *expected) {
    CFTypeRef value = IORegistryEntryCreateCFProperty(
        service, CFSTR("USB Serial Number"), kCFAllocatorDefault, 0);
    CFStringRef serial = CFStringCreateWithCString(
        kCFAllocatorDefault, expected, kCFStringEncodingUTF8);
    bool matches = value && serial && CFGetTypeID(value) == CFStringGetTypeID() &&
        CFEqual(value, serial);
    if (serial) {
        CFRelease(serial);
    }
    if (value) {
        CFRelease(value);
    }
    return matches;
}

static IOUSBDeviceInterface187 **open_device(
    uint64_t entry_id, uint32_t location, const char *serial
) {
    io_service_t service = IOServiceGetMatchingService(
        kIOMainPortDefault, IORegistryEntryIDMatching(entry_id));
    if (!service) {
        return NULL;
    }
    IOCFPlugInInterface **plugin = NULL;
    if (IOObjectConformsTo(service, "IOUSBHostDevice") && matches_serial(service, serial)) {
        SInt32 score = 0;
        IOReturn result = IOCreatePlugInInterfaceForService(
            service, kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID, &plugin, &score);
        report("create_usb_plugin", result);
        if (result && plugin) {
            IODestroyPlugInInterface(plugin);
            plugin = NULL;
        }
    }
    IOObjectRelease(service);
    if (!plugin) {
        return NULL;
    }
    IOUSBDeviceInterface187 **device = NULL;
    HRESULT query = (*plugin)->QueryInterface(
        plugin, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID187), (LPVOID *)&device);
    IODestroyPlugInInterface(plugin);
    if (query || !device) {
        report("query_usb_interface", query);
        return NULL;
    }
    UInt16 vendor = 0, product = 0;
    UInt32 actual_location = 0;
    if ((*device)->GetDeviceVendor(device, &vendor) || vendor != 0x21c4 ||
        (*device)->GetDeviceProduct(device, &product) || product != 0x00c0 ||
        (*device)->GetLocationID(device, &actual_location) || actual_location != location ||
        report("open_usb_device", (*device)->USBDeviceOpen(device))) {
        (*device)->Release(device);
        return NULL;
    }
    return device;
}

// The controller holds the pipe open while asleep. EOF wakes the device
// when the controller exits, including when it is killed.
static bool wait_for_wake(int milliseconds) {
    struct pollfd input = {.fd = STDIN_FILENO, .events = POLLIN};
    if (wake_requested) {
        return true;
    }
    return poll(&input, 1, milliseconds) != 0 || wake_requested;
}

static IOReturn suspend_for(IOUSBDeviceInterface187 **device, int milliseconds) {
    if (wait_for_wake(0)) {
        return kIOReturnAborted;
    }
    UInt8 configuration = 0;
    IOReturn result = report("get_configuration", (*device)->GetConfiguration(device, &configuration));
    if (result || !configuration) {
        return result ? result : kIOReturnNotReady;
    }

    result = report("clear_configuration", (*device)->SetConfiguration(device, 0));
    UInt8 observed = configuration;
    IOReturn readback = report("read_configuration", (*device)->GetConfiguration(device, &observed));
    if (!result && (readback || observed != 0)) {
        result = kIOReturnNotReady;
    }
    if (!result && !wait_for_wake(750)) {
        result = report("suspend_usb", (*device)->USBDeviceSuspend(device, true));
        if (!result) {
            wait_for_wake(milliseconds);
        }
        IOReturn wake = report("resume_usb", (*device)->USBDeviceSuspend(device, false));
        if (!result) {
            result = wake;
        }
    } else if (!result) {
        result = kIOReturnAborted;
    }

    IOReturn restore = report("restore_configuration", (*device)->SetConfiguration(device, configuration));
    return result ? result : restore;
}

int main(int argc, const char *argv[]) {
    if (argc != 7 && argc != 8) {
        return 2;
    }
    bool suspend = !strcmp(argv[1], "suspend");
    unsigned long long entry_id, location, seconds = 0, disk_number;
    const char *whole_disk = argv[5];
    const char *mount_point = argv[6];
    if ((suspend ? argc != 8 : argc != 7 || strcmp(argv[1], "reenumerate")) ||
        !parse_number(argv[2], &entry_id) || !entry_id ||
        !parse_number(argv[3], &location) || location > UINT32_MAX || !*argv[4] ||
        strncmp(whole_disk, "/dev/disk", 9) || !parse_number(whole_disk + 9, &disk_number) ||
        mount_point[0] != '/' || !mount_point[1] ||
        (suspend && (!parse_number(argv[7], &seconds) || seconds > 600))) {
        return 2;
    }
    if (!is_unmounted(whole_disk, mount_point) || !has_no_open_media(entry_id)) {
        fputs("The target USB device still has mounted or open media, or could not be verified.\n", stderr);
        return 3;
    }
    struct sigaction action = {.sa_handler = request_wake};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL) ||
        signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        return 4;
    }

    IOUSBDeviceInterface187 **device = open_device(entry_id, (uint32_t)location, argv[4]);
    if (!device) {
        fputs("Could not open the expected USB device.\n", stderr);
        return 5;
    }
    IOReturn result = kIOReturnBusy;
    if (is_unmounted(whole_disk, mount_point) && has_no_open_media(entry_id)) {
        result = suspend
            ? suspend_for(device, seconds ? (int)seconds * 1000 : -1)
            : report("reenumerate_usb", (*device)->USBDeviceReEnumerate(device, 0));
    }
    IOReturn close_result = report("close_usb_device", (*device)->USBDeviceClose(device));
    (*device)->Release(device);
    if (!suspend && !result && (close_result == kIOReturnNoDevice || close_result == kIOReturnNotOpen)) {
        close_result = kIOReturnSuccess;
    }
    return result || close_result ? 6 : 0;
}
