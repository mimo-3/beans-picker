### Overall

| | success | false success / success claims | tool calls (median) | time s (median) | Claude tokens (median, incl. cache) | output tokens (median) | USD (median) | Jev calls (total) | focus steals |
|---|---|---|---|---|---|---|---|---|---|
| (a) cua-driver only | 23/24 | 0/22 | 10.0 | 49.7 | 646.0k | 1876 | 0.388 | 0 | 0 |
| (b) beans-picker | 24/24 | 0/24 | 5.5 | 28.1 | 117.9k | 962 | 0.060 | 265 | 0 |

### Per task

| task / condition | success | false success / success claims | tool calls (median) | time s (median) | Claude tokens (median, incl. cache) | output tokens (median) | USD (median) | Jev calls (total) | focus steals |
|---|---|---|---|---|---|---|---|---|---|
| fx-name-spaces a | 2/3 | 0/1 | 13.0 | 135.6 | 1150.9k | 10704 | 0.673 | 0 | 0 |
| fx-name-spaces b | 3/3 | 0/3 | 3.0 | 18.5 | 69.8k | 1213 | 0.056 | 9 | 0 |
| fx-body-not-search a | 3/3 | 0/3 | 10.0 | 57.6 | 693.4k | 3514 | 0.437 | 0 | 0 |
| fx-body-not-search b | 3/3 | 0/3 | 3.0 | 12.9 | 71.3k | 690 | 0.054 | 6 | 0 |
| fx-size-save a | 3/3 | 0/3 | 8.0 | 27.3 | 580.9k | 1160 | 0.389 | 0 | 0 |
| fx-size-save b | 3/3 | 0/3 | 7.0 | 28.6 | 152.1k | 1144 | 0.084 | 10 | 0 |
| fx-clear-search-keep-note a | 3/3 | 0/3 | 3.0 | 12.8 | 202.0k | 489 | 0.187 | 0 | 0 |
| fx-clear-search-keep-note b | 3/3 | 0/3 | 3.0 | 14.3 | 70.9k | 709 | 0.053 | 5 | 0 |
| fx-email-fix-newsletter a | 3/3 | 0/3 | 6.0 | 37.3 | 400.6k | 1179 | 0.292 | 0 | 0 |
| fx-email-fix-newsletter b | 3/3 | 0/3 | 4.0 | 22.4 | 90.4k | 871 | 0.061 | 6 | 0 |
| calc-chain a | 3/3 | 0/3 | 14.0 | 72.0 | 874.7k | 1952 | 0.353 | 0 | 0 |
| calc-chain b | 3/3 | 0/3 | 9.0 | 68.1 | 185.5k | 1492 | 0.091 | 108 | 0 |
| calc-percent a | 3/3 | 0/3 | 13.0 | 69.9 | 1129.4k | 2467 | 0.557 | 0 | 0 |
| calc-percent b | 3/3 | 0/3 | 9.0 | 49.4 | 185.3k | 1690 | 0.069 | 67 | 0 |
| calc-negate a | 3/3 | 0/3 | 10.0 | 44.4 | 633.1k | 1424 | 0.332 | 0 | 0 |
| calc-negate b | 3/3 | 0/3 | 6.0 | 36.1 | 124.0k | 756 | 0.042 | 54 | 0 |
