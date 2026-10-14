(function(root) {
  const normalize = value =>
    typeof value === 'string' && /^[A-Za-z0-9._]{1,30}$/.test(value) ? value.toLowerCase() : null;

  function latestByUsername(visits, now = Date.now()) {
    const latest = new Map();
    for (const visit of visits) {
      const username = normalize(visit.username),
        observed = Date.parse(visit.time);
      if (
        !username ||
        visit.simulated !== false ||
        !['classroom-ip-v1', 'classroom-ip-v2'].includes(visit.consent_version) ||
        !visit.ip ||
        !Number.isFinite(observed) ||
        observed > now ||
        observed <= now - 86400000
      )
        continue;
      const previous = latest.get(username);
      if (!previous || observed > Date.parse(previous.time)) latest.set(username, visit);
    }
    return latest;
  }

  function senderUsername(event) {
    if (event.direction === 'outgoing') return null;
    const source = event.source || '';
    if (
      !['dm', 'comment'].includes(event.kind) &&
      !String(event.id || '').startsWith('dm:') &&
      !(['image', 'audio', 'video'].includes(event.kind) && source.includes('direct message'))
    )
      return null;
    if (event.sender?.username) return normalize(event.sender.username);
    return normalize(source.match(/^@([A-Za-z0-9._]{1,30})(?=\s|$)/)?.[1]);
  }

  const api = { latestByUsername, senderUsername };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.IPSharing = api;
})(typeof window !== 'undefined' ? window : globalThis);
