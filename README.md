
## What it does

- **Time Counting:** After 1 minute 15 seconds with no
  mouse or keyboard activity, counting pauses (and that quiet time isn't counted). Notifications
  at 5 and 1 minutes left, then the computer locks every 5 seconds until more time is added
  (typing a coupon or the supervisor password holds the lock off until 7 seconds after the last key).
- **Extra time:** coupons the child can redeem, or time the supervisor adds.
- **Web filter:** Moderated and Limited lock SafeSearch and check every website against
  Cloudflare for Families. Restricted allows approved sites only. A blocked site closes
  the browsers and shows the Falcon Advisor box.
- **Protection:** a supervisor password, an activity log, settings the child's account
  can't change, and a guard that runs as SYSTEM every minute.

## Install

1. Install Python 3 from [python.org](https://www.python.org/downloads/) and tick
   "Add python.exe to PATH".
2. Put `build.bat`, `falcon.py`, `web.html`, `logon.mp3`, `logoff.mp3` and `error.mp3` in one folder.
3. Right-click `build.bat` and choose **Run as administrator**. It:
   - builds `FalconGuard.exe` with PyInstaller and installs it in `C:\Program Files\FalconGuard`
   - asks which Windows account the child uses
   - locks `C:\ProgramData\Falcon` so only administrators can change the settings
   - schedules the guard to run every minute as SYSTEM
   - starts FalconGuard at every logon (it only acts on the child's account)
   - adds Start menu shortcuts (Settings, Dashboard, Self-Test, Uninstall) and lists FalconGuard
     in Settings > Apps
4. In FalconGuard Settings, create the supervisor password and click **Turn On** on the
   General tab.
5. Sign in to the child's account and run **FalconGuard Self-Test** once.

The child's account should be a standard account, not an administrator.

To remove FalconGuard, use **Settings > Apps > FalconGuard > Uninstall**, the
**Uninstall FalconGuard** shortcut, or `uninstall.bat`. Windows asks for administrator rights
(not the supervisor password), and you choose whether to keep the settings and activity log.

## Viewing and changing settings

**FalconGuard Settings** opens for anyone, without administrator rights or a password, so the
child can see the rules. It opens **view only**: changing anything, or clicking
**Edit Settings...**, asks for the supervisor password. After that the window saves through
FalconGuard's background service (the web dashboard service, which runs as SYSTEM), so no
administrator rights are needed. The settings file itself stays read-only for everyone except
administrators.

## Web dashboard (port 7675)

Parents on the same Wi-Fi can open FalconGuard in a browser. It looks and works like the
window on the PC, and it needs the supervisor password too.

- `build.bat` starts it at every boot (as SYSTEM, so it can save the protected settings), opens
  port 7675 in Windows Firewall for Private networks only, and prints the addresses to use,
  for example `http://192.168.1.20:7675`.
- Only devices on the local network can connect. Requests from the internet are refused.
- 3 wrong passwords lock that device out for 5 minutes. Sessions end after 30 minutes without use.
- Every change made in the browser is logged with "(web, address)".
- The child can open **Dashboard...** without the password to see their time and redeem coupons.
- The PC's network must be set to **Private**, or other devices can't reach it.
- The connection isn't encrypted, so the password crosses the home network as plain text.
  Use it on a network you trust, and don't set up port forwarding to it on your router.

## Subscription (My Account)

- **My Account** shows the edition, the monthly price, the expiry date and the days remaining.
- 3 days before it ends, a warning banner appears. 1 day before, a critical banner appears.
- On the day it ends, protection turns off and can't be turned back on until the date in the
  gist is moved forward. Click **Check Now** on My Account after renewing.
- The last answer is saved, so the expiry date still applies when the gist can't be reached.

## Command-line options

| Command | What it does |
|---|---|
| `FalconGuard.exe` | Settings (asks for the supervisor password) |
| `--dashboard` | The child's Dashboard: time left and coupons |
| `--monitor` | The background part on the child's account (started at logon) |
| `--guard` | One protection check (scheduled every minute as SYSTEM) |
| `--protect` | Sets the settings folder permissions (administrator) |
| `--set-child NAME` | Chooses the child's account (administrator) |
| `--selftest` | Checks each Windows feature on this PC |
| `--web` | The web dashboard on port 7675 (started at boot as SYSTEM) |
| `--setup NAME` | Used by `build.bat`: protection, tasks, firewall, shortcuts, Apps entry (administrator) |
| `--uninstall` | Removes FalconGuard (asks Windows for administrator rights) |
