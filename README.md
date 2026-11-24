# Loader

Uploader and package installer for the M5Stack Cardputer Zero. The image already has a Files app. Loader is the one that receives files over FTP or a browser and installs a selected `.deb` with `dpkg`.

The package is architecture `all` (Python). Build it on Windows. The first copy still has to reach the device by `scp`, because this app is what provides FTP after that.

## Build

```
python build_deb.py
```

Output: `dist/cz-files_1.0.0_all.deb`

## First install

The account on the device must be able to use `sudo`, and it must be in the `sudo` group. That is the normal account created by the imager.

```
scp dist/cz-files_1.0.0_all.deb USER@DEVICE:/tmp/
ssh USER@DEVICE "sudo dpkg -i /tmp/cz-files_1.0.0_all.deb && systemctl --user restart APPLaunch"
```

Open **Loader** in the launcher. The built-in Files app does not install packages.

The package writes `/etc/sudoers.d/cz-files`. It allows the `sudo` group to run `/usr/bin/dpkg` and `/usr/bin/apt-get` with no password. Install tries that first. A password line appears only when sudo refuses. The password is passed to `sudo -S` for that install and is not saved. The web install page shows the same box only in that case.

## On the device

| Key | Action |
|---|---|
| `F` / `X`, or `fn`+`F` / `fn`+`X` | move |
| `Z` / `C`, or `fn`+`Z` / `fn`+`C` | parent folder / open |
| `OK` / `Enter` | open a folder, or install a `.deb`. A sudo password is asked only when sudo requires one. |
| `d` or `fn`+Backspace | delete the selected file, or an empty folder |
| `r` | rename the selected file or folder |
| `m` | create a folder in the current directory |
| `Tab` | menu: open, install, rename, delete, new folder, network, uploads |
| `PgUp` / `PgDn` (`fn`+`L` / `fn`+`M`) | page |
| `n` | network page: `F` / `X` picks user, password, FTP port, or web port. `OK` edits, type, `OK` saves and restarts both servers. Short `Esc` cancels the field. |
| `h` | jump to `~/incoming` |
| `Esc` | back |
| hold `Esc` for 3 seconds | the bottom bar fills, then the app quits |

Uploads appear in `~/incoming`. `OK` on a `.deb`, then `OK` again to install. If `dpkg` reports missing dependencies, the app runs `apt-get install -f -y`. Hold `Esc` afterward so the launcher picks up a new icon.

The full command output is kept in `~/.cache/cz-files/last-install.log`.

## From Windows

The top right of the screen is the battery percent from `/sys/class/power_supply`. A `+` means charging. `--` means the gauge is not there.

The network page shows the address. Change user, password, or either port there. The web port cannot be `8080`. Defaults, also written to `~/.config/cz-files/config`:

- user `card`
- password `cardputer`
- FTP port `2121`
- web port `8081`

FTP, from the same Wi-Fi:

```
ftp://card:cardputer@DEVICE:2121/
```

Web, in a browser: `http://DEVICE:8081`. The page asks for the same user and password. It opens in `~/incoming` and can move through the rest of the disk. From a row you can download, rename, delete, or install a `.deb`. Upload and New folder go into the folder you are viewing. A folder deletes only when it is empty.

FTP stays in `~/incoming`. The web page is the file browser.

Both servers speak cleartext and listen on all interfaces. Use them on your own network.

## Screen preview

From this folder, with Pillow installed (`pip install pillow`):

```
python src/main.py --screenshot preview
```
