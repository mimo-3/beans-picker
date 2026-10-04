// Brings one running app back to the front. Usage: activate <pid>
#import <AppKit/AppKit.h>

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    if (argc != 2) return 2;
    NSRunningApplication *app = [NSRunningApplication runningApplicationWithProcessIdentifier:(pid_t)atoi(argv[1])];
    return app && [app activateWithOptions:0] ? 0 : 1;
  }
}
