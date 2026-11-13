# bazaar-contrib-system-service

User-level host service management for
[Bazaar Compute Node](https://github.com/yuchanns/bazaar-compute-node): a systemd
user unit on Linux, a launchd agent on macOS and a scheduled task on Windows.

## Setup

Add the Contrib to a node, register the node to start at login, then start it:

```sh
bcn contrib add system-service --enable
bcn system-service install
bcn system-service start
```

The service runs the node with the configuration the install command used. It
reads its environment from `~/.config/bcn/runtime.env`, or `runtime.ps1` on
Windows, when that file exists; `install --env-file PATH` selects another.
When an agent upgrades the node, the node exits and the service starts it
again on the new release.

## Commands

- `bcn system-service install [--env-file PATH] [--force]`: register the
  service and start it at the next login. It refuses to overwrite service
  files, or on Windows a scheduled task, that bcn did not create; `--force`
  replaces them.
- `bcn system-service start` / `restart`: start the node through the native
  service manager and wait until it is ready.
- `bcn system-service stop`: stop the service.
- `bcn system-service status`: report registration, activity and the node's
  health.
- `bcn system-service uninstall`: remove the service and its managed files.

`bcn start`, `bcn stop` and `bcn restart` forward to these commands.
