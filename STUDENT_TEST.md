# Classroom IP test

This feature records the initial address and IP changes after a participant
explicitly starts automatic checks on the test page. It cannot obtain or verify the IP used for an Instagram DM.
Student usernames are self-reported, not authenticated Instagram identities.

## Run with phones

`start-student-test.cmd` runs the Windows setup and normal dashboard/tunnel
launcher with IP sharing enabled (`start-dashboard.cmd -StudentTest`).
Connect Instagram through its official OAuth flow. Credentials and tokens stay
local. The IP-sharing page is served separately from the OAuth tunnel.
For an IP-only check, Instagram login and the media-analysis providers are not required.

1. Stop the existing dashboard with Ctrl+C, then run `start-student-test.cmd`.
   To test without connecting to Instagram, run `start-student-test.cmd --no-monitor`
   after installing the Python requirements. This mode does not start a tunnel.
2. Open `http://127.0.0.1:8765` on this computer. Expand **IP sharing settings**
   to see the phone links. Each launch generates a new link.
3. Connect the phones and this computer to the same Wi-Fi. Copy a phone link
   from the panel and share it with the consenting test participants.
4. Each participant opens that link, enters the same username as their Instagram account, checks the consent
   box and selects **Agree and start automatic checks**. Opening the page alone
   does not save a record. The visible page checks every 15 seconds and saves
   only the initial IP and subsequent changes, for up to one hour.
5. Message cards refresh every five seconds and show the latest shared IP
   matching their sender's username (case-insensitive). The address is labelled
   as shared via the test page, with its observation time and an unverified
   username match. Unmatched messages show **IP: Not shared**. Own-post media
   does not receive a sender IP. There is no separate visit list or demo display.
6. **Clear shared IP records**, under **IP sharing settings**, deletes these
   records and removes their IPs from the message cards.

The IP on an older message card is the latest shared address for that username,
not the address used when that message was sent. Anyone can enter a username
on the consent page, so this association does not establish account ownership.

**Stop automatic checks** ends the student's session and turns off requests.
Closing or reloading the page stops checking; consent is required again to start.
Checks pause when the page is hidden, and mobile browsers can suspend it.
There is no background app, service worker, or tracking after the page closes.
The session also expires on the server after one hour, even if the client fails
to stop. Session secrets are kept in memory and are not added to saved records.

If the server becomes unreachable, the page shows that no new IP was confirmed
and retries while visible. It resumes after reconnection if the session is still
valid. Leaving the classroom Wi-Fi usually makes the local link unreachable:
automatic checks do not solve that limitation. Public HTTPS hosting is needed
to keep checking across Wi-Fi/mobile-data changes. It is not deployed here.

Clearing saved visits deletes history; it does not revoke active student consent
sessions. Active students can generate new records if their IP changes. Use their
Stop button or stop the classroom server to end those sessions.

If Windows asks about network access, allow Python on the classroom's private
network. If the page will not open, check the selected LAN address and whether
the Wi-Fi blocks communication between devices. Phones on mobile data cannot
reach the computer's private Wi-Fi address. The page is served on port 8766;
the dashboard stays on this computer's loopback interface, port 8765.

## What the address means

- A phone connecting over the same LAN normally shows its **local/private IP**.
- A direct connection to a publicly hosted server can expose the visitor's
  **public IP**, possibly shared with other students or belonging to a VPN.
- This implementation records the socket peer and ignores forwarded-IP headers.
  Behind a tunnel or reverse proxy it will show the proxy address. Public hosting
  needs HTTPS and an explicitly configured, trusted proxy before forwarded IPs
  can be used. No public deployment or tunnel is included here.
- Neither result proves the address used for an Instagram message, identifies
  a person, or provides their exact location. No location is requested.

Only the entered username, connection IP, timestamp and consent/source metadata
are saved. The separate collector exposes the consent page and submission route;
it cannot serve the dashboard, credentials, or saved visits. The classroom token
is carried in the link fragment and submitted with consent, not in access logs.

Records are stored in `tools/student-test/visits.json` (excluded from Git),
limited to 500 records. Expired records are removed on reads and approximately
once per minute while the dashboard runs. If the app is stopped, deletion resumes
when it starts again. Closing the app does not immediately delete unexpired records.

## Offline checks

```powershell
.venv/Scripts/python.exe examples/student_test_test.py
.venv/Scripts/python.exe examples/sender_profiles_test.py
.venv/Scripts/python.exe dashboard.py --self-test
node examples/ip_sharing_test.js
```

To compare saved IPs with actual TCP source addresses on this computer, run the
following with its current Wi-Fi IPv4 address (shown by `ipconfig`):

```powershell
.venv/Scripts/python.exe examples/ip_capture_network_test.py --lan-ip 192.168.1.8
```

This uses a temporary collector and temporary records, checks loopback/LAN source
changes and spoofed-IP rejection, and writes a report to
`tools/student-test/network-verification.json`. It does not modify live visits.
It also demonstrates that the same entered username can be accepted from another
connection: an address match is not proof of Instagram account ownership.
Separate phone validation requires comparing a consenting phone's captured
address with the IPv4 address in that phone's Wi-Fi settings.
