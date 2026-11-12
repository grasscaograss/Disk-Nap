#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOMessage.h>
#include <IOKit/pwr_mgt/IOPMLib.h>
#include <IOKit/ps/IOPowerSources.h>
#include <IOKit/ps/IOPSKeys.h>

#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

static io_connect_t root_port;

static void report_power_source(void *context) {
    (void)context;
    static const char *last_event;
    CFTypeRef info = IOPSCopyPowerSourcesInfo();
    CFStringRef source = info ? IOPSGetProvidingPowerSourceType(info) : NULL;
    const char *event = "power-source-unknown";
    if (source && CFEqual(source, CFSTR(kIOPSBatteryPowerValue))) {
        event = "power-source-battery";
    } else if (source && CFEqual(source, CFSTR(kIOPSACPowerValue))) {
        event = "power-source-ac";
    }
    if (event != last_event) {
        puts(event);
        last_event = event;
    }
    if (info) {
        CFRelease(info);
    }
}

static void power_changed(void *context, io_service_t service, natural_t message, void *argument) {
    (void)context;
    (void)service;
    switch (message) {
        case kIOMessageCanSystemSleep:
            IOAllowPowerChange(root_port, (intptr_t)argument);
            break;
        case kIOMessageSystemWillSleep: {
            report_power_source(NULL);
            puts("will-sleep");
            struct pollfd reply = {.fd = STDIN_FILENO, .events = POLLIN};
            if (poll(&reply, 1, 18000) > 0 && (reply.revents & POLLIN)) {
                char ack;
                if (read(STDIN_FILENO, &ack, 1) != 1) {
                    CFRunLoopStop(CFRunLoopGetCurrent());
                }
            }
            IOAllowPowerChange(root_port, (intptr_t)argument);
            break;
        }
        case kIOMessageSystemHasPoweredOn:
            report_power_source(NULL);
            puts("did-wake");
            break;
    }
}

static void controller_input(CFFileDescriptorRef descriptor, CFOptionFlags flags, void *context) {
    (void)flags;
    (void)context;
    struct pollfd input = {.fd = STDIN_FILENO, .events = POLLIN};
    if (poll(&input, 1, 0) > 0) {
        if (input.revents & (POLLHUP | POLLERR)) {
            CFRunLoopStop(CFRunLoopGetCurrent());
            return;
        }
        if (input.revents & POLLIN) {
            char late_ack;
            if (read(STDIN_FILENO, &late_ack, 1) != 1) {
                CFRunLoopStop(CFRunLoopGetCurrent());
                return;
            }
        }
    }
    CFFileDescriptorEnableCallBacks(descriptor, kCFFileDescriptorReadCallBack);
}

int main(void) {
    IONotificationPortRef notifications = NULL;
    io_object_t notifier = IO_OBJECT_NULL;
    setvbuf(stdout, NULL, _IOLBF, 0);
    root_port = IORegisterForSystemPower(NULL, &notifications, power_changed, &notifier);
    if (!root_port) {
        fputs("Could not register for macOS power events.\n", stderr);
        return 1;
    }
    CFRunLoopRef loop = CFRunLoopGetCurrent();
    CFRunLoopSourceRef events = IONotificationPortGetRunLoopSource(notifications);
    CFRunLoopSourceRef power_sources = IOPSNotificationCreateRunLoopSource(report_power_source, NULL);
    CFFileDescriptorRef control = CFFileDescriptorCreate(
        kCFAllocatorDefault, STDIN_FILENO, false, controller_input, NULL);
    CFRunLoopSourceRef pipe = control ? CFFileDescriptorCreateRunLoopSource(kCFAllocatorDefault, control, 0) : NULL;
    if (events && pipe) {
        CFRunLoopAddSource(loop, events, kCFRunLoopDefaultMode);
        CFRunLoopAddSource(loop, pipe, kCFRunLoopDefaultMode);
        if (power_sources) {
            CFRunLoopAddSource(loop, power_sources, kCFRunLoopDefaultMode);
        }
        CFFileDescriptorEnableCallBacks(control, kCFFileDescriptorReadCallBack);
        puts("ready");
        report_power_source(NULL);
        CFRunLoopRun();
        CFRunLoopRemoveSource(loop, pipe, kCFRunLoopDefaultMode);
        CFRunLoopRemoveSource(loop, events, kCFRunLoopDefaultMode);
    }
    if (power_sources) {
        CFRunLoopSourceInvalidate(power_sources);
        CFRelease(power_sources);
    }
    if (pipe) {
        CFRelease(pipe);
    }
    if (control) {
        CFFileDescriptorInvalidate(control);
        CFRelease(control);
    }
    IODeregisterForSystemPower(&notifier);
    IOServiceClose(root_port);
    IONotificationPortDestroy(notifications);
    return events && pipe ? 0 : 1;
}
