# MaxRead Deployment

This folder contains migration/deployment helpers. Real secrets must stay outside GitHub.

For Windows migration, use `deploy/windows/README.md`. It includes a Windows env template, PowerShell install script, Feishu auth notes, file/cache migration choices, and a step-by-step task plan.

## Prepare a local key file

On the target machine, create a file such as `~/maxread.env` from `deploy/env.keys.example` and fill real values:

```bash
cp deploy/env.keys.example ~/maxread.env
chmod 600 ~/maxread.env
$EDITOR ~/maxread.env
```

At minimum, set `OPENAI_API_KEY`. Feishu bot credentials are managed by `lark-cli`; run `lark-cli doctor` and `lark-cli whoami` on the target machine. Do not put the app secret, OAuth state, database, or paper cache in this repository. See [`../docs/operations-and-migration.md`](../docs/operations-and-migration.md) for the complete environment and migration contract.

## One-command install

```bash
bash deploy/install.sh
```

The script asks for:

- deploy directory, for example `/opt/maxread` or `~/maxread`
- local key/env file path, for example `~/maxread.env`

It then clones/updates the repo, copies the key file to `<deploy-dir>/.env`, creates a Python venv, installs MaxRead, and writes run scripts. Service startup defaults to `no`; set `MAXREAD_AUTO_START=yes` only after the target has passed health checks. The independent duty reminder is never started by this installer.

The installer writes user-service templates for a normal user account. On a
root-owned Linux host such as the Aliyun target, use the `*-system.service`
templates with `User`, `Group`, and `HOME` set explicitly. Install the units and
run `systemctl daemon-reload` first; start them only during the listener cutover
window. The duty reminder has its own unit and remains outside this cutover.

## Manual run

```bash
cd <deploy-dir>
./run-listener.sh
./run-admin.sh
```

Admin UI defaults to `http://127.0.0.1:8765/`.


## Fresh machine bootstrap

For a machine with no checkout yet, put `MAXREAD_GITHUB_TOKEN` in the local key file or export it in the shell, then run a copy of `deploy/bootstrap.sh`:

```bash
bash bootstrap.sh
```

The bootstrap script clones the private repo using a temporary Git askpass helper, then removes that helper. The token is not written into the remote URL.
