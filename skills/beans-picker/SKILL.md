---
name: beans-picker
description: Operate a macOS app window (native apps or pages in Chrome) in the background through the beans-picker MCP server (observe / act / extract). Use when a task asks to fill in forms, click through a flow, or read values in a Mac window and the beans-picker tools are available. Covers which steps need Jev and which run faster without it (candidateId and batched `then` steps), when cua-driver fits better, how to read act's status, and recipes for combo boxes, number fields, pop-ups, long tables and checking the result before reporting success.
---

# Driving a Mac window with beans-picker

beans-picker works on the accessibility tree, never on pixels you choose, and never brings the app to
the front. Every `act` step is checked on fresh snapshots: did the effect asked for happen? Text is
entered as given and checked by exact equality.

## Which tool, and when to ask Jev

Three ways to take a step, from fastest to most help:

| way | Jev? | use it for |
|---|---|---|
| `act` with `candidateId` (and `then` for the next steps) | no | Steps whose control you already saw in an observe: fields to fill, boxes to tick, Next / Save, keys. |
| `act` or `observe` with an `instruction` | yes | Picking one control among look-alikes by what surrounds it: "Restore in the row deleted 2026-09-24", "the second Jordan Lee (Security)". A long page where the plain candidate list would be huge. |
| `extract` | yes | Reading one value or one table exactly, on a crowded page. |

Each Jev call takes about a second and is billed by input tokens; a plain `observe` and a
`candidateId` step make no Jev call. So ask Jev when the choice is the hard part, and skip it when
the choice is already made.

**Tasks that do not need Jev at all**: a form with labelled fields, a wizard of Next buttons, a
settings page, a keyboard drag (focus the handle, Space, arrows, Space). One plain observe gives
the ids (the first 80 by default: when `total` is larger than the list you got, pass a larger
`limit`); one or two `act` calls with `then` do the rest.

**Tasks where Jev earns its time**: many similar rows (files, payments, tickets) where the right one
is known only by its content; decoys (two people with one name, a stale duplicate); reading a
value out of a busy page.

**Use cua-driver instead** (screenshots and pixel clicks) when the UI has no accessibility tree to
speak of: a canvas, a game, custom-drawn widgets. Also for mouse drags, for anything you must *see*
(an image, a layout), and for a menu-bar command that has no keyboard shortcut (beans-picker reports it
as `failed` with `foreground_required`).

## The fast loop

1. **observe** the window once, with the task's `pid` / `windowId` and no instruction. You get
   candidate ids with what each one does, plus `screenText` and `fields`.
2. **act** on everything you can already name, in one call: the first step in the top-level fields,
   the rest in `then` (`[{instruction, candidateId, text?}, …]`). The run stops at the first step
   that is not `done` or `unverified`, and `steps` says what each one did. The step that commits
   (Save, Submit) can be the last in `then` when the steps before it are routine.
3. **Read the status** of the last step (and `steps` when it stopped early). When the action
   brought up new controls (a dialog, a menu, the next step of a form), `newCandidates` lists them
   with their ids: act on those directly. Observe again only when what you need is not there.
4. Name a step with an `instruction` (Jev) only when the id is not in hand or several controls look
   alike.

| status | what to do |
|---|---|
| `done` | The effect is seen. Go on. |
| `unverified` | The field changed but its exact text could not be read. `extract` proves the exact text only when the element it returns has `exact: true`; otherwise it may be trimmed, so treat the text as unconfirmed (running `beans-picker grant-ax` lets it be read). |
| `no_effect` | Nothing changed. Do not repeat the same call blindly. Observe again: the control may be disabled, hidden or off-screen. Focusing a drag handle also reads as `no_effect`; the next key press still goes to it. |
| `mismatch` | Something changed, but not what was asked. Read `change` and fix it before going on. |
| `ambiguous` | Pick the right `candidateId` from the list returned and call again. Do not guess by rewording. |
| `needs_confirmation` | The action may not be undoable. Repeat it with `allowDestructive: true` only if the task asks for exactly this. |
| `not_found` | Observe again. What you want may be off-screen (scroll) or behind another step. |
| `failed` | Read `code`. `foreground_required` and `foreground_violation` mean this action needs cua-driver. |

Ids are stable: an id from an earlier `observe` still works while that control is on the window,
including in later steps of the same `then`. Look-alike controls (the Edit button of each section,
the menu button of each list row) are told apart by the nearest heading or text: `click Button
"Edit" near "Company"`.

## Recipes

**Text fields.** Give the text in `text`. `set_value` replaces the whole field, `type_into` inserts
at the caret, and `append` adds at the end. Whitespace is kept.

**Number fields** (`Incrementor`, an `<input type="number">`). Use `set Incrementor "…" to "3"`
with `text: "3"`, or the one-step up / down candidates.

**Pop-ups** (`PopUpButton`, an HTML `<select>`). Use the `choose … in PopUpButton` candidate
with `text` set to the option's exact title: it opens the menu, presses the item and closes the
menu in one step. If a pop-up's menu is open anyway (`window.modal` is a Menu), press the item you
want once, and never press Return in it: Return takes the highlighted item, which may be another.

**Combo boxes with suggestions** (reviewers, assignees, tags):
1. Type a distinctive part of the name into the combo box.
2. `observe` again. Each suggestion is a candidate `choose option "…" in List "…"`.
3. Act on the option whose whole text matches: team, handle, e-mail. Two people can share a name.
4. **Do not press Return to pick.** Return takes whichever suggestion is highlighted, and that is
   often the first one, not the one you mean.

**Right-click menus.** Commands such as rename, star or move to trash may live only in an item's
context menu. Act on its `open the context menu of … (right-click)` candidate, or press Shift+F10
after selecting the item. The menu's items then appear as candidates; press the one you want.

**Shift-click and cmd-click.** To extend a selection over a range (or add one item to it), act on
the item's click or toggle candidate with `modifiers: ["shift"]` (or `["cmd"]`). The item must be
visible on the window. A row click can report `unverified`: its selection is often not in the
tree, so check the selection count or the next step instead of clicking again.

**Long lists and tables.** Rows outside the visible area are not in the tree at all (virtualized
grids). Narrow the list first: use the search field, a filter, or a sort header (sort by amount to
find the largest). Then read it with `extract` on the table, which returns `rows`. If the row you
need is still not there, act on a `scroll … down one page` candidate and read again.

**Reading values.** `extract` returns the text as read. A field's value is exact to the character
only when the element has `exact: true`; without it, whitespace at the ends may be missing. For a whole table or list, name the table
("the Transactions table", or "the table of rooms" when it has no name). In `screenText` a table
row is one line, its cells joined by ` | `; so is a row of a web list (its heading and the short
texts after it). When the result is `ambiguous`, the shortlist already carries each
element's value or rows, so read them from there.

**Dialogs and sheets.** While one is open, only its own controls are offered (`window.modal` names
it). Finish it or dismiss it with Escape before going back to the page.

## Before you report success

- The last action that commits the task (Save, Submit, Issue refund, Request review) returned
  `done`.
- A confirmation is visible: `extract` or observe the message ("Saved", "Refund of $34.00
  issued"), or the list now shows the new state.
- For tasks with several parts (several tabs, several items), check every part, not only the last.
- If you could not confirm it, say so. An honest "not done" beats a false "done".
