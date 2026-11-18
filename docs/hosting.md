# Hosting guide

The controller needs one always-on Linux machine with Python 3.11 or newer and outbound internet
access: it calls MELCloud, the price feed and the weather service every 15 minutes, and nothing needs
to reach it from the internet. Any small VM, a Raspberry Pi or a home server will do.

This guide describes one setup that costs nothing to run:

- **The machine:** an Oracle Cloud "Always Free" VM running Ubuntu.
- **The phone page:** served privately over Tailscale, with no open ports.
- **Alerting:** a Healthchecks.io heartbeat that emails you when the controller stops.

Providers change their consoles, menus and free-tier terms, so the steps below say what to set up
and why, and link to each provider's own documentation for the clicks.

## 1. The machine

Create a VM on Oracle Cloud's free tier ([Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)):

- Pick an Always Free–eligible shape and the current **Ubuntu LTS** image. The smallest shape is
  plenty: a run takes a few seconds every 15 minutes.
- Log in with an **SSH key**. The default user on Ubuntu images is `ubuntu`, and the paths below
  assume the app in `/home/ubuntu/melcloud-spot-heating`; adjust them if yours differ.
- Leave every inbound port closed except SSH. Once Tailscale is set up (section 3), you can close
  SSH to the internet too and reach the machine over Tailscale instead.
- Oracle may stop or reclaim idle free-tier instances, and any VM can fail. The heartbeat
  (section 4) tells you when that happens.

Then on the machine, install and check it as in the [README's quick start](../README.md#quick-start),
cron line included, and set the machine's time zone so cron and `ls` times match the logs:

```sh
sudo timedatectl set-timezone <your zone>
```

## 2. Updating

See [Running it](operations.md#updating). Copying the data to your own computer for the savings
report: [the savings estimate](operations.md#the-savings-estimate).

## 3. The phone page over Tailscale

[Tailscale](https://tailscale.com/kb) puts your phone and the VM on one private network (a
"tailnet"). The page listens only on `127.0.0.1:8080`, and `tailscale serve` publishes it with HTTPS
to your tailnet alone, so nothing is open to the internet. Tailscale tells the page who is asking,
and only the logins you list get in. Keep the page on `127.0.0.1`: it trusts that login header, so
anything else that can reach its port could claim to be you.

1. Install Tailscale on the VM ([Linux install](https://tailscale.com/download/linux)),
   run `sudo tailscale up` and sign in.
2. Enable HTTPS certificates for your tailnet in the Tailscale admin console, then publish the page:
   `sudo tailscale serve --bg 8080`. `tailscale serve status` prints its address
   (`https://<machine>.<tailnet>.ts.net`). See [Tailscale Serve](https://tailscale.com/kb/1312/serve)
   if the command has changed.
3. `cp web.env.example web.env && chmod 600 web.env`, and fill it in:
   - `WEB_USERS`: the Tailscale logins allowed in, each with the nickname the page and `web.log` show
     (`login=nickname,...`); use a first name or initial, not an email.
   - `WEATHER_LATITUDE` and `WEATHER_LONGITUDE`: the same two lines as in `.env`, and with ENTSO-E
     prices `ENTSOE_TOKEN` too, since the page plans its chart itself.

   The page's own process does not load `.env`: it writes the control files and starts `run.sh`,
   which loads `.env` in a child process. It runs as the same user, so this keeps the password out
   of the page's memory and logs, not beyond its reach: one more reason to keep it behind Tailscale.
4. Run the page as a service. Create `/etc/systemd/system/spot-heating-web.service`:

   ```ini
   [Unit]
   Description=melcloud-spot-heating phone page
   After=network-online.target

   [Service]
   User=ubuntu
   WorkingDirectory=/home/ubuntu/melcloud-spot-heating
   EnvironmentFile=/home/ubuntu/melcloud-spot-heating/web.env
   ExecStart=/home/ubuntu/melcloud-spot-heating/.venv/bin/python web.py
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```

   Then `sudo systemctl enable --now spot-heating-web`.
5. Invite the other people in your household to the tailnet. They install the Tailscale app on
   their phones and sign in, open the address, and add it to the home screen (on an iPhone: Safari,
   Share, Add to Home Screen).

## 4. Alerting

Nothing else watches an unattended controller, so give it a dead man's switch: a service that
expects a ping every 15 minutes and emails you when the pings stop.

1. Create a check at [Healthchecks.io](https://healthchecks.io) (the free tier is enough) with a
   period of **15 minutes** and a grace time of **about an hour**, so one slow run doesn't alert.
2. Put its ping URL in `.env`: `HEARTBEAT_URL='https://hc-ping.com/<your-check-uuid>'`.

A stopped cron, a lost VM and a run that keeps failing all end up as an email;
[running it](operations.md#when-something-fails) says exactly which runs ping. Any service that
accepts a plain HTTP GET works the same way.

For a closer look when an alert comes in, check, in this order:

- `tail ilp_optimizer.log`: each run's result and any errors;
- `crontab -l`: whether the cron line is still there;
- `systemctl status spot-heating-web`: whether the page is running.
