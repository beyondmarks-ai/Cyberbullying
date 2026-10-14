const assert = require('assert');
const { latestByUsername, senderUsername } = require('../dashboard/ip-sharing');
const now = Date.parse('2026-10-14T12:00:00Z');
const visit = (username, ip, age = 1000, extra = {}) => ({
  username,
  ip,
  time: new Date(now - age).toISOString(),
  simulated: false,
  consent_version: 'classroom-ip-v2',
  ...extra,
});

const records = [visit('Alice', '192.168.1.20', 2000), visit('ALICE', '192.168.1.21'), visit('bob', '192.168.1.22')];
const latest = latestByUsername(records, now);
assert.equal(latest.get('alice').ip, '192.168.1.21');
assert.equal(latest.get('bob').ip, '192.168.1.22');
assert.equal(latest.size, 2);
assert.equal(records.length, 3);

assert.equal(
  latestByUsername(
    [
      visit('demo', '192.0.2.42', 1000, { simulated: true }),
      visit('missing_flag', '192.0.2.43', 1000, { simulated: undefined }),
      visit('no_consent', '192.168.1.5', 1000, { consent_version: undefined }),
      visit('expired', '192.168.1.6', 86400000),
      visit('future', '192.168.1.7', -1000),
      visit('invalid', '192.168.1.8', 1000, { time: 'invalid' }),
    ],
    now,
  ).size,
  0,
);

assert.equal(senderUsername({ kind: 'dm', sender: { username: 'ALICE' }, source: '@old_name' }), 'alice');
assert.equal(senderUsername({ kind: 'dm', source: '@Alice' }), 'alice');
assert.equal(senderUsername({ kind: 'comment', source: '@Alice · post ABC' }), 'alice');
assert.equal(senderUsername({ kind: 'audio', source: '@Alice · direct message' }), 'alice');
assert.equal(senderUsername({ kind: 'dm', source: 'Username unavailable (ID: 123)' }), null);
assert.equal(senderUsername({ kind: 'image', source: 'post ABC', sender: { username: 'alice' } }), null);
assert.equal(senderUsername({ kind: 'dm', source: '@alice<script>' }), null);
assert.equal(latest.get(senderUsername({ kind: 'dm', source: '@alice2' })), undefined);
console.log(
  'IP matching checks passed: latest address, case-insensitive exact names, consent, expiry, simulated data exclusion, message types and missing usernames.',
);
