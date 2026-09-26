// Prints the key equivalents of an app's main menu as JSON, read from its main nib.
// Usage: menukeys <App.app path> [--nib <name>] [-AppleLanguages '(en)']
// The nib is instantiated in this process only, so the target app is never messaged or activated.
// Its bindings point at the app's own classes, which are missing here, so they are stubbed out.
#import <AppKit/AppKit.h>
#import <objc/runtime.h>

@implementation NSObject (MenuKeysStandIns)
- (id)mk_valueForUndefinedKey:(NSString *)key { return nil; }
- (void)mk_setValue:(id)value forUndefinedKey:(NSString *)key {}
- (void)mk_bind:(NSString *)binding toObject:(id)o withKeyPath:(NSString *)k options:(NSDictionary *)opts {}
@end

static void swap(SEL a, SEL b) {
  method_exchangeImplementations(class_getInstanceMethod([NSObject class], a), class_getInstanceMethod([NSObject class], b));
}

static void installStandIns(void) {
  swap(@selector(bind:toObject:withKeyPath:options:), @selector(mk_bind:toObject:withKeyPath:options:));
  method_exchangeImplementations(class_getInstanceMethod([NSObject class], @selector(valueForUndefinedKey:)),
                                 class_getInstanceMethod([NSObject class], @selector(mk_valueForUndefinedKey:)));
  method_exchangeImplementations(class_getInstanceMethod([NSObject class], @selector(setValue:forUndefinedKey:)),
                                 class_getInstanceMethod([NSObject class], @selector(mk_setValue:forUndefinedKey:)));
}

static NSArray<NSString *> *modNames(NSEventModifierFlags m) {
  NSMutableArray *out = [NSMutableArray array];
  if (m & NSEventModifierFlagControl) [out addObject:@"ctrl"];
  if (m & NSEventModifierFlagOption) [out addObject:@"option"];
  if (m & NSEventModifierFlagShift) [out addObject:@"shift"];
  if (m & NSEventModifierFlagCommand) [out addObject:@"cmd"];
  return out;
}

// `top` 0 is the application menu, whose run-time title is the app's name, not the nib's.
static void walk(NSMenu *menu, NSArray<NSString *> *prefix, NSInteger top, NSMutableArray *out) {
  NSInteger i = 0;
  for (NSMenuItem *item in menu.itemArray) {
    NSInteger here = prefix.count ? top : i++;
    if (item.isSeparatorItem || item.isHidden) continue;
    NSString *title = item.title ?: @"";
    if (!title.length && item.submenu) title = item.submenu.title ?: @"";
    NSArray *path = [prefix arrayByAddingObject:title];
    if (item.keyEquivalent.length && !item.isAlternate) {
      [out addObject:@{@"path": path, @"key": item.keyEquivalent, @"mods": modNames(item.keyEquivalentModifierMask), @"top": @(here)}];
    }
    if (item.submenu) walk(item.submenu, path, here, out);
  }
}

static void emit(NSArray *items) {
  NSData *d = [NSJSONSerialization dataWithJSONObject:items options:0 error:nil];
  fwrite(d.bytes, 1, d.length, stdout);
  fputc('\n', stdout);
}

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    NSString *app = nil, *name = nil;
    for (int i = 1; i < argc; i++) {
      if (!strcmp(argv[i], "--nib") && i + 1 < argc) name = [NSString stringWithUTF8String:argv[++i]];
      else if (argv[i][0] == '-') i++;  // a user-defaults argument such as -AppleLanguages '(en)'
      else app = [NSString stringWithUTF8String:argv[i]];
    }
    if (!app) {
      fprintf(stderr, "usage: menukeys <App.app> [--nib <name>]\n");
      return 2;
    }
    [NSApplication sharedApplication];
    installStandIns();
    NSBundle *bundle = [NSBundle bundleWithPath:app];
    if (!name) name = [bundle objectForInfoDictionaryKey:@"NSMainNibFile"];
    NSMutableArray *items = [NSMutableArray array];
    @try {
      NSNib *nib = name ? [[NSNib alloc] initWithNibNamed:name bundle:bundle] : nil;
      NSArray *objects = nil;
      if (nib && [nib instantiateWithOwner:nil topLevelObjects:&objects]) {
        NSMenu *main = nil;
        NSInteger best = -1;
        for (id o in objects) {
          if (![o isKindOfClass:[NSMenu class]]) continue;
          NSInteger subs = 0;
          for (NSMenuItem *i in [(NSMenu *)o itemArray]) if (i.submenu) subs++;
          if (subs > best) { best = subs; main = o; }
        }
        if (main) walk(main, @[], 0, items);
      }
    } @catch (NSException *e) {
      fprintf(stderr, "menukeys: %s\n", e.reason.UTF8String ?: "exception");
    }
    emit(items);
  }
  return 0;
}
