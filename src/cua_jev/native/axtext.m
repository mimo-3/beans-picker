// Prints the exact state of one app's editable text elements (text areas, text fields, search
// fields, combo boxes, number fields and steppers) and toggles (checkboxes, radio buttons), as JSON:
// {"fields":[{"window":"<title>","document":"<file URL>","role":"AXTextField","title":"<AXTitle or AXDescription>","value":"<text or 0/1>"}]}
// in tree order. Read-only: it only copies accessibility attributes and never messages, activates
// or raises the app. Secure (password) fields are skipped. cua-driver trims whitespace from values,
// reports a placeholder as the value of an empty field and leaves out a checkbox's state; this is
// how cua-jev reads them exactly.
// Usage: axtext <pid> [<output file>]   (without a file, JSON goes to stdout)
//        axtext --prompt                (asks macOS to list this helper under Accessibility)
#import <ApplicationServices/ApplicationServices.h>
#import <Foundation/Foundation.h>

static id copyAttr(AXUIElementRef e, CFStringRef name) {
  CFTypeRef v = NULL;
  if (AXUIElementCopyAttributeValue(e, name, &v) != kAXErrorSuccess || v == NULL) return nil;
  return CFBridgingRelease(v);
}

static BOOL editable(NSString *role) {
  return [role isEqualToString:(NSString *)kAXTextAreaRole] || [role isEqualToString:(NSString *)kAXTextFieldRole] ||
         [role isEqualToString:(NSString *)kAXComboBoxRole] || [role isEqualToString:@"AXSearchField"] ||
         [role isEqualToString:(NSString *)kAXIncrementorRole];
}

static BOOL toggle(NSString *role) {
  return [role isEqualToString:(NSString *)kAXCheckBoxRole] || [role isEqualToString:(NSString *)kAXRadioButtonRole];
}

static NSString *str(id v) { return [v isKindOfClass:[NSString class]] ? v : nil; }

static void walk(AXUIElementRef e, int depth, NSString *window, NSString *document, NSMutableArray *out) {
  if (depth > 25 || out.count >= 200) return;
  NSString *role = str(copyAttr(e, kAXRoleAttribute));
  if (role && (editable(role) || toggle(role))) {
    NSString *subrole = str(copyAttr(e, kAXSubroleAttribute));
    if ([subrole isEqualToString:(NSString *)kAXSecureTextFieldSubrole]) return;
    id value = copyAttr(e, kAXValueAttribute);
    NSString *text = [value isKindOfClass:[NSString class]] ? value : [value isKindOfClass:[NSNumber class]] ? [value stringValue] : nil;
    NSString *title = str(copyAttr(e, kAXTitleAttribute)) ?: str(copyAttr(e, kAXDescriptionAttribute)) ?: @"";
    if (text) [out addObject:@{@"window" : window, @"document" : document, @"role" : role, @"title" : title, @"value" : text}];
    if (editable(role)) return;
  }
  NSArray *children = copyAttr(e, kAXChildrenAttribute);
  // A table lists its cells under its rows and again under its columns: only the rows are walked,
  // as cua-jev keeps only those, so that both list the same fields in the same order.
  BOOL hasRows = NO;
  for (id c in children) hasRows = hasRows || [str(copyAttr((__bridge AXUIElementRef)c, kAXRoleAttribute)) isEqualToString:(NSString *)kAXRowRole];
  for (id c in children) {
    if (hasRows && [str(copyAttr((__bridge AXUIElementRef)c, kAXRoleAttribute)) isEqualToString:(NSString *)kAXColumnRole]) continue;
    walk((__bridge AXUIElementRef)c, depth + 1, window, document, out);
  }
}

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    if (argc >= 2 && strcmp(argv[1], "--prompt") == 0) {
      NSDictionary *opts = @{(__bridge NSString *)kAXTrustedCheckOptionPrompt : @YES};
      printf("%s\n", AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)opts) ? "trusted" : "not_trusted");
      return 0;
    }
    if (argc < 2) return 2;
    NSString *outFile = argc >= 3 ? [NSString stringWithUTF8String:argv[2]] : nil;
    NSData *json;
    if (!AXIsProcessTrusted()) {
      json = [@"{\"error\":\"not_trusted\"}" dataUsingEncoding:NSUTF8StringEncoding];
    } else {
      pid_t pid = (pid_t)atoi(argv[1]);
      AXUIElementRef app = AXUIElementCreateApplication(pid);
      NSMutableArray *out = [NSMutableArray array];
      NSArray *windows = copyAttr(app, kAXWindowsAttribute);
      for (id w in windows) {
        NSString *title = str(copyAttr((__bridge AXUIElementRef)w, kAXTitleAttribute)) ?: @"";
        NSString *document = str(copyAttr((__bridge AXUIElementRef)w, kAXDocumentAttribute)) ?: @"";
        walk((__bridge AXUIElementRef)w, 0, title, document, out);
      }
      CFRelease(app);
      json = [NSJSONSerialization dataWithJSONObject:@{@"fields" : out} options:0 error:nil];
    }
    if (outFile) {
      [json writeToFile:outFile atomically:YES];
    } else {
      fwrite(json.bytes, 1, json.length, stdout);
      printf("\n");
    }
  }
  return 0;
}
