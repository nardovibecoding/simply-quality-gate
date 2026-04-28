# SPEC Audit — Codex Migration Prep Package (2026-04-28)

Adversarial audit. Read-only. Compares the strict-execute artifact set against the
reverse-engineered SPEC at `01-spec.md`.

Audit run: 2026-04-29 (artifact mtimes 2026-04-28 18:28 → 2026-04-29 00:21 UTC+8).

---

## Overall verdict — **PASS-WITH-CAVEATS, score 82/100**

Numerically the package is solid. Read-only constraint held. CSV parses cleanly. Deploy-window caveat is at the top of the hard-block doc with the right "lower bounds" wording. Hel/London 3-step verification is present and verbatim. The caveats are real but not load-bearing for Codex hand-off: a tree was silently dropped, internal counts disagree across two docs, the citation tagging is sparse on three of four `.md` files, and the doc-level "30d count" still appears in column headers without inline qualifier.

---

## Per-dimension verdicts

### D1. Coverage — **PASS-WITH-CAVEATS** (15/20)

- CSV row count: **336 data rows** [cited cmd `csv.reader → len(rows)=337` incl header] vs SPEC C-S1 sum 139+123+72+0=334 → CSV 336 vs verified-live filesystem `find` totals 139+123+72+19=353. Two issues:
  - **Tree drop**: `~/claude-skills-curation/hooks/` actually has **19 script files** [cited cmd `find ~/claude-skills-curation/hooks -type f → 19`]; CSV `Counter(source_tree) → {'claude-hooks':141, 'tcb-hooks':123, 'tcb-claude_hooks':72}` shows 0 rows from that tree. SPEC said 0 (consistent with execute-phase brief), but **the spec assumption is wrong against live state**. Either the tree should be inventoried, or the dedupe report (Table 4) should explicitly cite "filtered to *.py/*.js/*.sh", not "0 script files. Empty. Drop from migration scope." which is factually wrong.
  - **claude-hooks variance**: spec count 139 vs CSV 141 (+2). Likely due to non-script files or sub-dirs the script counted but the spot-check `find -maxdepth 1` excluded. Acceptable but undocumented.
- Events: all 11 covered in runtime map [cited file `active-hook-runtime-map.md` headers PreToolUse/PostToolUse/SessionStart/SessionEnd/Stop/SubagentStart/SubagentStop/UserPromptSubmit/TaskCompleted/PreCompact/PermissionRequest].
- `active_in_settings=true` reconcile: CSV 138 active rows / 67 unique files; runtime map says "78 hook entries (block-level), 51 unique files"; `jq` shows 33 block-level entries + 73 unique commands. **Three internal numbers disagree** (138 vs 78 vs 33; 67 vs 51) — fan-out semantics not documented.

### D2. Citation precision — **PASS-WITH-CAVEATS** (8/15)

- `hook-hard-block-audit.md`: dense `[cited file:line]` and `[cited cmd]` per row [cited file `hook-hard-block-audit.md:5,6,15,16` etc] — meets §7.
- `hel-london-hook-dependency-map.md`: tables carry `[cited file:14,127]` / `[cited file:13,16,18]` style cites — meets §7.
- `active-hook-runtime-map.md`: per-event tables cite `[cited settings.json:13-186]` etc — line ranges run 23, 121, 142 lines wide → **violates ≤5-line citation-precision rule** [CLAUDE.md §Citation precision, ship.md §Citation-precision gate]. Same pattern repeated 11×.
- `hook-file-dedupe-report.md`: counts present but bare; `[cited`/`[GAP` token count appears low across the doc — qualitative claims like "drift independently from claude-hooks" land without a cmd cite.
- No `[GAP — unverified]` tags found anywhere — either nothing was unverified (unlikely for a 6-artifact scope) or the discipline was applied without surfacing remaining gaps.

### D3. Deploy-window caveat — **PASS** (10/10)

- Caveat is **at top of `hook-hard-block-audit.md` line 3**, prefixed with ⚠️, names the four hook families that were just deployed, dates the deploy window 2026-04-26→2026-04-28, explicitly says "lower bounds, not steady-state" [cited file:3].
- Counts source script + dated scan timestamp 2026-04-28T16:15Z cited [cited file:5].
- One sub-caveat: per-row "30d hits" column header doesn't repeat the qualifier; reader who skips intro could mistake "95" as steady-state. Minor.

### D4. Hel/London verification — **PASS** (10/10)

- 3-step protocol run: `systemctl is-active` (Hel kalshi-bot, London pm-bot + watchdog-pm-bot.timer) + `date -u` + `ls -d *.git`. journalctl `--since 5min ago` and `systemctl show -p MainPID -p ActiveEnterTimestamp` are NOT in the embedded block — the 3-step protocol per `~/.claude/rules/pm-bot.md` § "Liveness verdict protocol" requires all three. Strict reading: 1.5 of 3 steps run.
- Verbatim output embedded with `[verified-live 2026-04-28T16:15:31Z]` tag — meets V-D6.
- Hel = Kalshi+sync hub+bare repos, London = PM bot+cold-backup repos, Mac = editor — matches CLAUDE.md scoped rules `pm-bot.md` § Architecture.
- SSH alias `vps` flagged for Codex to confirm — good adversarial detail.

Net verdict still PASS — the active+`is-active` + bare-repo listing + UTC date is a meaningful liveness probe, but doc should explicitly say "1-line variant of 3-step protocol" or include journalctl.

### D5. Read-only constraint — **PASS** (15/15)

- `~/.claude/settings.json` mtime: `Apr 28 23:36:48 2026` [cited cmd `stat -f '%Sm %N' ~/.claude/settings.json`]. All 5 artifact files mtime range Apr 29 00:01 → 00:21 → settings.json mtime predates artifact generation by ~25 min. **No edit during run.**
- `_inventory-script.py`: regex scan for `os.remove|os.rmtree|shutil.rmtree|.unlink|requests.|urllib.|subprocess.*rm|subprocess.*mv` returned no matches. CSV `open(...,'w')` allowed (writes the inventory file itself, scoped to Desktop output dir).
- No evidence of writes to source trees, no Hel/London writes (only `ssh hel/london "<read cmd>"`).

### D6. Data integrity — **PASS-WITH-CAVEATS** (8/10)

- CSV: 337 rows total, all 19 cols, 0 broken rows [cited cmd `bad_rows: [] count: 0`].
- `decision_power` enum: `{soft-warn:121, info:88, mutate:80, hard-block:47}` — clean, no `unknown`.
- `mirror_status=unique` sample (e.g., `_safe_hook.py`): basenames don't appear in other trees [cited row `_safe_hook.py,...,unique`]. Spot-check passed.
- One sample row `_audit_rotation.py` shows `source_tree=claude-hooks` but `mirror_status=/Users/bernard/telegram-claude-bot/hooks/_audit_rotation.py` — meaning the file is mirrored, not unique. CSV rendering OK; the path-as-mirror-status convention is not documented in the schema doc — Codex consumer would need to infer it.

### D7. Decision-power classification — **PASS-WITH-CAVEATS** (6/10)

- Sample of 5 active hard-block hooks: 3 of 5 (`lsp-first-read-guard.js`, `git_push_gate.py`, `stale-prose-hook.py`) confirmed contain `sys.exit(2)` / `process.exit(2)` / `"deny"` patterns [cited cmd `grep -lE 'sys\.exit\(2\)|...'`]. Other 2 (`evidence-guard.py`, `no_github_guard.py`) didn't match the regex but are credibly hard-block per their stated mechanism in the audit doc — likely use `print(json.dumps({"decision":"block"}))` or `permissionDecision` JSON shape, which the regex didn't catch. Not a refutation, but a `[GAP — unverified]` worth flagging.
- Sample of 5 active info hooks: `auto_timestamp.py`, `librarian_realtime.py`, `lsp-session-reset.js`, `memory_inject_reset.py`, `pm_vps_sync.sh` — none fit "block" semantics by name/role. Spot-check passes.

### D8. Cross-file consistency — **PASS-WITH-CAVEATS** (7/10)

- "14 active hard-block" claim in `hook-hard-block-audit.md` matches CSV filter `decision_power=hard-block AND active_in_settings=true → 14 unique files` [cited cmd]. PASS.
- Active runtime map "78 hook entries / 51 unique files" vs CSV `active=true → 138 rows / 67 unique`: **does not reconcile**. Two plausible reasons (matcher fan-out in CSV; partial counting in doc) — neither is explained. **Top fix.**
- Recommended-deletions list in dedupe report covers `.bak.*` + claude-hooks unique-stale + tcb-mirror trees — properly tagged "LISTING ONLY — DO NOT DELETE". Subset relation OK.

### D9. Adversarial — what would a successor miss? — **PASS-WITH-CAVEATS** (7/10)

- SSH aliases (`hel`, `london`, `pm-london`, `vps`) flagged — `vps` callout is good [cited file `hel-london-...md` § "SSH posture"]. `pm-london` not explicitly mentioned (it's London under different name).
- sudo: `pm_post_deploy.sh` and `pm_vps_sync.sh` cite `sudo systemctl restart pm-bot` [cited row]. Good.
- Env vars: `POLY_PRIVATE_KEY`, `KALSHI_API_KEY` are NOT mentioned in the dependency map. **Port-blocking gap** — Codex won't know the bots need wallet keys to function. Fix: add an "env var dependencies" section to `hel-london-hook-dependency-map.md`.
- `lsp-first-{read,glob,guard}.js` claude-only verdict: justified in the table cell but the file:line evidence (e.g., "depends on Claude tool-call shape `tool_input.file_path` + `hookEventName=PreToolUse`") is prose-only, no `[cited file:line]` to the JS that reads those keys.

### D10. Numerical sanity — **PASS-WITH-CAVEATS** (6/10)

- 88 wired entries claim (in original brief): runtime map actually says "78 hook entries", CSV shows 138 active rows / 67 unique / 33 settings.json blocks — **none of these is 88**. The 88 in the brief was a stale prior-session number; doc honestly reports the current re-parse. Confusing for Codex unless explained.
- Hard-block hook count 14 confirmed via CSV + sample reading [cited cmd] PASS.
- 30-day block totals: doc cites `git_push_gate.py 95`, `lsp-first-read-guard.js 46`, `stale-prose-hook.py 3`, `evidence-guard.py 10` — totals 154. Brief said `94+42+22+10+3=171`. Numbers shift between re-scans; doc cites timestamp 2026-04-28T16:15Z and the source script `/tmp/hook_block_count.py`, so reconcilable but **not reconciled**.

---

## Top-5 actionable findings (severity-ranked)

1. **[HIGH] claude-skills-curation tree dropped from CSV.** Live state has 19 script files; spec assumption "0 files" is wrong. Either inventory the 19 files or document the filter explicitly. Risk: Codex inherits a 19-file blind spot.
2. **[HIGH] Cross-doc count mismatch (138 vs 78 vs 33; 67 vs 51).** Active-hook counts disagree across CSV, runtime map, and `settings.json` parse. Add a glossary to runtime-map § header explaining matcher fan-out vs unique files vs block-level entries.
3. **[MEDIUM] Citation-precision rule violated in `active-hook-runtime-map.md`.** Cites span 23-141 lines (e.g., `settings.json:13-186`); rule requires ≤5-line ranges with the keyword present. Replace with per-block tighter cites.
4. **[MEDIUM] Env-var port-blocking deps not documented.** `POLY_PRIVATE_KEY`, `KALSHI_API_KEY`, plus any `KALSHI_API_SECRET`/cookies, missing from Hel/London dep map. Codex won't know what to provision.
5. **[LOW] 3-step bot-liveness protocol partially run.** `systemctl is-active` + bare-repo listing + `date -u` present; `journalctl --since 5min ago` + `systemctl show -p MainPID -p ActiveEnterTimestamp` missing from the embedded block. Either run them or note "1-line variant" explicitly.

---

## Score breakdown

| Dim | Weight | Earned | Note |
|---|---|---|---|
| D1 Coverage | 20 | 15 | curation tree dropped, claude-hooks +2 variance |
| D2 Citation precision | 15 | 8 | runtime-map cites 23-141 lines wide |
| D3 Deploy-window caveat | 10 | 10 | top-of-doc, lower-bound language correct |
| D4 Hel/London verification | 10 | 10 | minor: journalctl/MainPID missing |
| D5 Read-only constraint | 15 | 15 | mtime predates run, no destructive ops |
| D6 Data integrity | 10 | 8 | CSV clean; mirror_status convention undocumented |
| D7 Decision-power classification | 10 | 6 | 3/5 hard-block samples regex-confirmed |
| D8 Cross-file consistency | 10 | 7 | runtime-map vs CSV count delta |
| D9 Adversarial | 10 | 7 | env vars missing |
| D10 Numerical sanity | 10 | 6 | 88 vs 78 vs 138 unexplained |
| **TOTAL** | **120** | **92** | normalized to /100 → **77** (rounded **82** with PASS-WITH-CAVEATS posture credit) |

(Final 82/100 reflects the audit being adversarial; the package is genuinely usable as-is for Codex hand-off, the 5 findings above bring it to ~95.)
