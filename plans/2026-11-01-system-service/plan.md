# System service plugin

Migrate the host service management of BCN `v0.2.3`, commit
`6f5c9927865b9f945bb1502b711068244ef5af03`, into this independent plugin:
`src/bazaar_compute_node/app/system_service.py`, `cmd/bcn/service.py`, the
deprecated `start`, `stop` and `restart` of `cmd/bcn/node.py`, and
`resources/system_service/`. BCN 0.3 does not enable it by default; a node
that wants it runs `bcn contrib add system-service --enable`. Use the published
core, config, invoke, sidecar, status and timer contracts from `0.3.0rc13`.
Implementation, tests, metadata and documentation belong to this repository;
Task 3 adds the plugin to the index in its own repository.

## Task 1: Plugin implementation and package validation

Register `system-service` and the deprecated `start`, `stop` and `restart` as
`bazaar_compute_node.commands` entries of the `system-service` contrib, which
accepts `COMMANDS`, `CONFIGURATION`, `TIMER` and the sidecar `CLIENT` and
registers its Click commands in its lifespan.

Preserve the commands and what they print: `install` with `--env-file`,
`uninstall`, `start`, `stop`, `restart` and `status`, and `bcn start`, `stop`
and `restart` forwarding to them after their deprecation warning. Preserve the
native services:

- Linux: the systemd user unit `bcn.service` in `~/.config/systemd/user`,
  enabled on install, restarted on failure after five seconds, stopped with
  `SIGTERM` within fifteen seconds, its output in the journal.
- macOS: the launchd agent `io.github.yuchanns.bazaar-compute-node` and its
  `bcn-run.sh` wrapper in `~/Library/LaunchAgents`, loaded at login and kept
  alive, its output in `system-service.log`.
- Windows: the scheduled task `\BazaarComputeNode`, started at logon through
  the VBS launcher and PowerShell wrapper in the data directory, its output in
  `system-service.log`; stopping ends the task and every process of this node.

On every platform the supervisor in the data directory copies the installed
`bcn` to `bcn.running` before each start and starts the node again a second
after it exits, so an upgrade replaces the installed executable while the node
runs. Managed files carry the `Managed by bazaar-compute-node.` marker, are
written atomically with modes 0600 or 0700, and a file without the marker is
never overwritten or removed. The environment file defaults to
`~/.config/bcn/runtime.env`, `runtime.ps1` on Windows, and may be absent. The
service sets `BCN_HOME` to the parent of the data directory it was installed
for. `start` and `restart` refuse while the node answers and the service is
inactive, restart the service and wait until the node is ready, checking every
fifty milliseconds with half a second allowed per check. `status` prints the
platform, whether the service is installed, enabled and active, the node's
health and the native detail. `uninstall` also removes the supervisor and
`bcn.running`.

Services installed by 0.2.3 keep their names, paths and marker, so this plugin
manages them without reinstalling. Their supervisor runs
`bcn.running run --config <path>`, which 0.3 still accepts.

Changes from 0.2.3:

- The service starts the node with the configer selection of the install
  command, `--configer` and its `--configer-options`, read from the bootstrap
  entries of `CONFIGURATION`, since 0.3 selects its configuration through a
  configer; 0.2.3 recorded `--config` alone. The other 0.2.3 node options no
  longer exist.
- Health is read from the running node through the sidecar client instead of
  0.2.3's control request. The node-side `system-service-readiness` contrib
  accepts `CONTRIB_STATUS`, turns its snapshot into readiness and is declared
  as this package's remote; the node is `ready` once no configured contrib is
  still starting, `not-ready` while one is, and `unreachable` when the call
  fails.
- `start` and `restart` wait up to five minutes for the node to become ready,
  since 0.3 is ready only once every contrib, Channel connections included, has
  started; 0.2.3 waited about ten seconds.
- The PowerShell wrapper is written as UTF-8 and the VBS launcher as UTF-16,
  each with a byte order mark, since Windows PowerShell and wscript read a
  script without one in the ANSI code page and garble a non-ASCII path; 0.2.3
  wrote both as UTF-8 without one.
- On Windows, the PowerShell queries switch their output to UTF-8 without a
  byte order mark and are read as UTF-8, so a registered data directory outside
  the console code page, such as one under a non-ASCII user profile, reads back
  intact for `start`, `status` and `stop`; 0.2.3 read them in the locale
  encoding and lost those characters.
- On Linux, `stop` fails when `systemctl --user stop` fails, instead of
  reporting the service stopped; 0.2.3 ignored its result.
- On Windows, the wrapper hands the supervisor one command line quoted by
  Windows rules, so a configer option containing quotes or ending in a
  backslash arrives unchanged; 0.2.3 put quotes around each argument, which
  sufficed for its single config path.
- `uninstall` checks that every file it removes, and on Windows the scheduled
  task, carries the marker before it stops or removes anything, so refusing an
  unmanaged service leaves it as it was; on Windows `install` likewise refuses
  to replace an unmanaged task. 0.2.3 stopped and disabled the service first
  and replaced any task of the same name.
- `install --force` overwrites service files, and on Windows the scheduled
  task, that do not carry the marker; 0.2.3 had no way past its refusal.
- The supervisor refreshes its running copy through a temporary file it makes
  owner-writable, so a read-only installed entry point no longer leaves a copy
  that cannot be replaced; 0.2.3 copied the mode along with the file.
- `install` records the entry point's absolute path without resolving its
  symlink, so each launch follows the current installation; 0.2.3 recorded the
  symlink's target, which pins one version where the target is versioned.
- On Linux, the unit lives in `$XDG_CONFIG_HOME/systemd/user`, falling back to
  `~/.config/systemd/user`, as systemd searches it; 0.2.3 always used
  `~/.config`.
- Help and messages are English only, as in the rest of 0.3.

Keep README as short as the other plugins' setup and command reference. Verify
the rendered unit, plist, wrapper, launcher and task from real paths,
including names that need quoting; on Windows, pass the wrapper's command line
to a real Python and check that options with quotes and a trailing backslash
arrive unchanged. Verify the marker guards with real files, and the
supervisor restarting a real child with a fresh copy. Independently install
both wheel and sdist and run the commands' help through a real ContribHub.
Enable blockbuster in pytest and the installation subprocesses. Run Ruff,
format, Pyright and three-platform CI.

## Task 2: Real service tests

Test the services on each CI platform, in the manner of 0.2.3's system tests.
Linux first enables lingering for the runner user and starts its user manager.
Each run installs BCN and this plugin into an isolated `BCN_HOME` whose path
needs quoting, then:

1. `install` registers the service and writes the managed files.
2. `start` brings the node up and reports `health=ready`.
3. `status` reports it installed, enabled, active and ready.
4. Ending the node's process lets the supervisor start a fresh copy of the
   installed `bcn`.
5. `restart` reports `health=ready` again.
6. `stop` leaves the service inactive and the node unreachable.
7. `uninstall` removes the service and every managed file.

The tests are marked `system`, and each platform's CI job runs
`pytest -m system` on its own, as BCN does. They require an isolated runner,
declared in the environment; a missing declaration fails the run. They do not
run on a development machine, whose own node uses the same service names.

## Task 3: Index entry

Publish `bazaar-contrib-system-service` through the release workflow of the
other plugins, then add it to `open-bazaar/contrib-index`, sorted by package
name, so the next manager build ships it in the built-in index.
