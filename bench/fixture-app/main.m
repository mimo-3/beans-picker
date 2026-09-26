// Bench fixture: an AppKit window whose control values are written to a JSON state file.
// Usage: CuaJevFixture --state <file.json> [--title <t>] [--body <b>] [--search <s>] [--name <n>] [--email <e>]
#import <AppKit/AppKit.h>

@interface Fixture : NSObject <NSApplicationDelegate, NSTextFieldDelegate, NSTextViewDelegate, NSSearchFieldDelegate>
@property(strong) NSWindow *window;
@property(strong) NSSearchField *search;
@property(strong) NSTextField *name;
@property(strong) NSTextField *email;
@property(strong) NSTextView *body;
@property(strong) NSButton *newsletter;
@property(strong) NSPopUpButton *size;
@property(strong) NSTextField *status;
@property(copy) NSString *statePath;
@property(assign) NSInteger saves;
@property(assign) NSInteger deletes;
@property(strong) NSData *lastWritten;
@end

@implementation Fixture

- (NSTextField *)label:(NSString *)text at:(NSRect)frame in:(NSView *)view {
  NSTextField *l = [NSTextField labelWithString:text];
  l.frame = frame;
  [view addSubview:l];
  return l;
}

- (void)applicationDidFinishLaunching:(NSNotification *)note {
  NSArray *args = [[NSProcessInfo processInfo] arguments];
  NSString *title = @"cua-jev fixture";
  NSMutableDictionary *initial = [NSMutableDictionary dictionary];
  for (NSUInteger i = 1; i + 1 < args.count; i++) {
    if ([args[i] isEqualToString:@"--state"]) self.statePath = args[i + 1];
    else if ([args[i] isEqualToString:@"--title"]) title = args[i + 1];
    else if ([args[i] hasPrefix:@"--"]) initial[[args[i] substringFromIndex:2]] = args[i + 1];
  }
  self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(80, 120, 520, 460)
                                            styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
                                              backing:NSBackingStoreBuffered
                                                defer:NO];
  self.window.title = title;
  NSView *v = self.window.contentView;

  self.search = [[NSSearchField alloc] initWithFrame:NSMakeRect(20, 420, 480, 24)];
  self.search.placeholderString = @"Search notes";
  self.search.delegate = self;
  [v addSubview:self.search];

  [self label:@"Name" at:NSMakeRect(20, 384, 60, 20) in:v];
  self.name = [[NSTextField alloc] initWithFrame:NSMakeRect(90, 382, 410, 24)];
  self.name.placeholderString = @"Full name";
  self.name.delegate = self;
  [v addSubview:self.name];

  [self label:@"Email" at:NSMakeRect(20, 350, 60, 20) in:v];
  self.email = [[NSTextField alloc] initWithFrame:NSMakeRect(90, 348, 410, 24)];
  self.email.placeholderString = @"name@example.com";
  self.email.delegate = self;
  [v addSubview:self.email];

  [self label:@"Size" at:NSMakeRect(20, 314, 60, 20) in:v];
  self.size = [[NSPopUpButton alloc] initWithFrame:NSMakeRect(90, 310, 160, 26) pullsDown:NO];
  [self.size addItemsWithTitles:@[ @"Small", @"Medium", @"Large" ]];
  self.size.target = self;
  self.size.action = @selector(changed:);
  [v addSubview:self.size];

  self.newsletter = [NSButton checkboxWithTitle:@"Send newsletter" target:self action:@selector(changed:)];
  self.newsletter.frame = NSMakeRect(270, 312, 200, 22);
  [v addSubview:self.newsletter];

  NSScrollView *scroll = [[NSScrollView alloc] initWithFrame:NSMakeRect(20, 80, 480, 220)];
  scroll.hasVerticalScroller = YES;
  scroll.borderType = NSBezelBorder;
  self.body = [[NSTextView alloc] initWithFrame:NSMakeRect(0, 0, 480, 220)];
  self.body.richText = NO;
  self.body.automaticQuoteSubstitutionEnabled = NO;
  self.body.automaticDashSubstitutionEnabled = NO;
  self.body.automaticTextReplacementEnabled = NO;
  self.body.automaticSpellingCorrectionEnabled = NO;
  self.body.string = initial[@"body"] ?: @"";
  self.body.delegate = self;
  [self.body setAccessibilityLabel:@"Note body"];
  scroll.documentView = self.body;
  [v addSubview:scroll];

  NSButton *save = [NSButton buttonWithTitle:@"Save" target:self action:@selector(save:)];
  save.frame = NSMakeRect(400, 36, 100, 30);
  [v addSubview:save];
  NSButton *del = [NSButton buttonWithTitle:@"Delete note" target:self action:@selector(deleteNote:)];
  del.frame = NSMakeRect(280, 36, 110, 30);
  [v addSubview:del];
  NSButton *clear = [NSButton buttonWithTitle:@"Clear search" target:self action:@selector(clearSearch:)];
  clear.frame = NSMakeRect(20, 36, 120, 30);
  [v addSubview:clear];

  self.status = [self label:@"Not saved" at:NSMakeRect(20, 10, 480, 20) in:v];
  [self.status setAccessibilityIdentifier:@"status"];

  self.search.stringValue = initial[@"search"] ?: @"";
  self.name.stringValue = initial[@"name"] ?: @"";
  self.email.stringValue = initial[@"email"] ?: @"";
  [self.window orderFront:nil];
  [self write];
  // AXValue writes bypass delegates, so the state file also polls the controls.
  [NSTimer scheduledTimerWithTimeInterval:0.2 repeats:YES block:^(NSTimer *t) { [self write]; }];
}

- (void)save:(id)sender {
  self.saves += 1;
  self.status.stringValue = [NSString stringWithFormat:@"Saved %ld time%@", (long)self.saves, self.saves == 1 ? @"" : @"s"];
  [self write];
}

- (void)deleteNote:(id)sender {
  self.deletes += 1;
  self.body.string = @"";
  self.status.stringValue = @"Note deleted";
  [self write];
}

- (void)clearSearch:(id)sender {
  self.search.stringValue = @"";
  [self write];
}

- (void)changed:(id)sender { [self write]; }
- (void)controlTextDidChange:(NSNotification *)n { [self write]; }
- (void)textDidChange:(NSNotification *)n { [self write]; }

- (void)write {
  if (!self.statePath) return;
  NSDictionary *state = @{
    @"search" : self.search.stringValue ?: @"",
    @"name" : self.name.stringValue ?: @"",
    @"email" : self.email.stringValue ?: @"",
    @"body" : self.body.string ?: @"",
    @"size" : self.size.titleOfSelectedItem ?: @"",
    @"newsletter" : @(self.newsletter.state == NSControlStateValueOn),
    @"saves" : @(self.saves),
    @"deletes" : @(self.deletes),
    @"status" : self.status.stringValue ?: @"",
  };
  NSData *json = [NSJSONSerialization dataWithJSONObject:state options:NSJSONWritingSortedKeys error:nil];
  if ([json isEqualToData:self.lastWritten]) return;
  self.lastWritten = json;
  [json writeToFile:self.statePath atomically:YES];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)app { return YES; }
@end

static NSMenu *mainMenu(void) {
  NSMenu *bar = [[NSMenu alloc] init];
  NSMenuItem *appItem = [[NSMenuItem alloc] init];
  [bar addItem:appItem];
  NSMenu *appMenu = [[NSMenu alloc] init];
  [appMenu addItemWithTitle:@"Quit CuaJevFixture" action:@selector(terminate:) keyEquivalent:@"q"];
  appItem.submenu = appMenu;
  NSMenuItem *editItem = [[NSMenuItem alloc] init];
  [bar addItem:editItem];
  NSMenu *edit = [[NSMenu alloc] initWithTitle:@"Edit"];
  [edit addItemWithTitle:@"Undo" action:@selector(undo:) keyEquivalent:@"z"];
  [edit addItemWithTitle:@"Cut" action:@selector(cut:) keyEquivalent:@"x"];
  [edit addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
  [edit addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
  [edit addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
  editItem.submenu = edit;
  return bar;
}

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    NSApplication *app = [NSApplication sharedApplication];
    app.activationPolicy = NSApplicationActivationPolicyRegular;
    app.mainMenu = mainMenu();
    Fixture *f = [[Fixture alloc] init];
    app.delegate = f;
    [app run];
  }
  return 0;
}
