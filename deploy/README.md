# MaxRead Deployment

This folder contains migration/deployment helpers. Real secrets must stay outside GitHub.

## Prepare a local key file

On the target machine, create a file such as `~/maxread.env` from `deploy/env.keys.example` and fill real values:

```bash
cp deploy/env.keys.example ~/maxread.env
chmod 600 ~/maxread.env
$EDITOR ~/maxread.env
```

At minimum, set `OPENAI_API_KEY`. Feishu credentials are managed by `lark-cli`; run `lark-cli doctor` and `lark-cli auth login` as needed on the target machine.

## One-command install

```bash
bash deploy/install.sh
```

The script asks for:

- deploy directory, for example `/opt/maxread` or `~/maxread`
- local key/env file path, for example `~/maxread.env`

It then clones/updates the repo, copies the key file to `<deploy-dir>/.env`, creates a Python venv, installs MaxRead, writes run scripts, and starts user services via systemd or launchd when available.

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
