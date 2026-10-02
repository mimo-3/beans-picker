// Records one window to an mp4 until SIGINT or SIGTERM. Usage: winrec <window id> <out.mp4>
// The window is captured on its own, so the video is the same whether it is covered or in front.
#import <AppKit/AppKit.h>
#import <AVFoundation/AVFoundation.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>

static SCStream *gStream;
static SCRecordingOutput *gOutput;
static id gRecorder;
static BOOL gStarted;
static BOOL gStopRequested;
static BOOL gStopping;
static int gStatus;

static void fail(NSString *message) {
  fprintf(stderr, "winrec: %s\n", message.UTF8String);
  exit(1);
}

// recordingOutputDidFinishRecording exits; this is for the case it never comes.
static void exitLater(void) {
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
    fprintf(stderr, "winrec: the file was not finished in time\n");
    exit(1);
  });
}

@interface Recorder : NSObject <SCStreamDelegate, SCRecordingOutputDelegate>
@end

@implementation Recorder
- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
  dispatch_async(dispatch_get_main_queue(), ^{
    if (gStopping) return;
    // The window closed, or capture was cut off: what was recorded so far is still written out.
    fprintf(stderr, "winrec: the recording stopped: %s\n", error.localizedDescription.UTF8String);
    gStopping = YES;
    gStatus = 1;
    exitLater();
  });
}
- (void)recordingOutputDidStartRecording:(SCRecordingOutput *)output {
  fprintf(stderr, "winrec: recording\n");
}
- (void)recordingOutput:(SCRecordingOutput *)output didFailWithError:(NSError *)error {
  fail([NSString stringWithFormat:@"the file could not be written: %@", error.localizedDescription]);
}
- (void)recordingOutputDidFinishRecording:(SCRecordingOutput *)output {
  exit(gStatus);
}
@end

static void stop(void) {
  if (gStopping) return;
  if (!gStream) fail(@"stopped before the recording started");
  // A stream still starting cannot be stopped: the start handler stops it.
  if (!gStarted) {
    gStopRequested = YES;
    return;
  }
  gStopping = YES;
  [gStream stopCaptureWithCompletionHandler:^(NSError *error) {
    if (error) fail([NSString stringWithFormat:@"could not stop: %@", error.localizedDescription]);
    exitLater();
  }];
}

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    if (argc != 3) fail(@"usage: winrec <window id> <out.mp4>");
    CGWindowID windowId = (CGWindowID)strtoul(argv[1], NULL, 10);
    NSURL *out = [NSURL fileURLWithPath:@(argv[2])];
    Recorder *recorder = [Recorder new];
    gRecorder = recorder;
    // ScreenCaptureKit shows its recording indicator through AppKit, on the main thread.
    [NSApplication sharedApplication];
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];

    static dispatch_source_t sources[2];
    int signals[2] = {SIGINT, SIGTERM};
    for (int i = 0; i < 2; i++) {
      signal(signals[i], SIG_IGN);
      sources[i] = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, signals[i], 0, dispatch_get_main_queue());
      dispatch_source_set_event_handler(sources[i], ^{ stop(); });
      dispatch_resume(sources[i]);
    }

    [SCShareableContent getShareableContentExcludingDesktopWindows:NO onScreenWindowsOnly:NO completionHandler:^(SCShareableContent *content, NSError *error) {
      dispatch_async(dispatch_get_main_queue(), ^{
      if (gStopping) return;
      if (!content) fail([NSString stringWithFormat:@"cannot read the window list (allow Screen Recording for the app running this command): %@", error.localizedDescription]);
      SCWindow *window = nil;
      for (SCWindow *w in content.windows) if (w.windowID == windowId) window = w;
      if (!window) fail([NSString stringWithFormat:@"no window with id %u", windowId]);

      SCContentFilter *filter = [[SCContentFilter alloc] initWithDesktopIndependentWindow:window];
      SCStreamConfiguration *config = [SCStreamConfiguration new];
      // H.264 wants even sizes.
      config.width = (size_t)(window.frame.size.width * filter.pointPixelScale) & ~(size_t)1;
      config.height = (size_t)(window.frame.size.height * filter.pointPixelScale) & ~(size_t)1;
      config.minimumFrameInterval = CMTimeMake(1, 30);
      config.showsCursor = NO;

      SCRecordingOutputConfiguration *file = [SCRecordingOutputConfiguration new];
      file.outputURL = out;
      file.outputFileType = AVFileTypeMPEG4;
      file.videoCodecType = AVVideoCodecTypeH264;
      gOutput = [[SCRecordingOutput alloc] initWithConfiguration:file delegate:recorder];

      SCStream *stream = [[SCStream alloc] initWithFilter:filter configuration:config delegate:recorder];
      NSError *added = nil;
      if (![stream addRecordingOutput:gOutput error:&added]) fail([NSString stringWithFormat:@"cannot record to %@: %@", out.path, added.localizedDescription]);
      gStream = stream;
      [stream startCaptureWithCompletionHandler:^(NSError *started) {
        if (started) fail([NSString stringWithFormat:@"cannot start: %@", started.localizedDescription]);
        dispatch_async(dispatch_get_main_queue(), ^{
          gStarted = YES;
          if (gStopRequested) stop();
        });
      }];
      });
    }];
    [NSApp run];
  }
}
