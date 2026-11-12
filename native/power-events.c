#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOMessage.h>
#include <IOKit/pwr_mgt/IOPMLib.h>

#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

static io_connect_t root_port;

static void power_changed(void *context, io_service_t service, natural_t message, void *argument) {
    (void)context;
    (void)service;
    switch (message) {
        case kIOMessageCanSystemSleep:
            IOAllowPowerChange(root_port, (intptr_t)argument);
            break;
        case kIOMessageSystemWillSleep: {
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
    CFFileDescriptorRef control = CFFileDescriptorCreate(
        kCFAllocatorDefault, STDIN_FILENO, false, controller_input, NULL);
    CFRunLoopSourceRef pipe = control ? CFFileDescriptorCreateRunLoopSource(kCFAllocatorDefault, control, 0) : NULL;
    if (events && pipe) {
        CFRunLoopAddSource(loop, events, kCFRunLoopDefaultMode);
        CFRunLoopAddSource(loop, pipe, kCFRunLoopDefaultMode);
        CFFileDescriptorEnableCallBacks(control, kCFFileDescriptorReadCallBack);
        puts("ready");
        CFRunLoopRun();
        CFRunLoopRemoveSource(loop, pipe, kCFRunLoopDefaultMode);
        CFRunLoopRemoveSource(loop, events, kCFRunLoopDefaultMode);
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
