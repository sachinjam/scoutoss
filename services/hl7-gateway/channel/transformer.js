// ScoutOSS HL7v2 -> clinical event JSON (Mirth source transformer, Rhino/E4X).
// Runs inside Mirth; the raw message contains PHI and is NEVER logged here. The indexer
// pseudonymizes PatientID with the site HMAC salt, shifts dates and scrubs free text.
function v(node) {
  if (node === undefined || node === null) return null;
  var text = node.toString();
  return text && text.length ? text : null;
}
function first(list) {
  return list.length() > 0 ? list[0] : undefined;
}

var type = v(msg['MSH']['MSH.9']['MSH.9.1']) + '^' + v(msg['MSH']['MSH.9']['MSH.9.2']);
var pid3 = first(msg['PID']['PID.3']);
var ev = {
  message_control_id: v(msg['MSH']['MSH.10']['MSH.10.1']) || ('' + new Date().getTime()),
  message_type: type,
  patient_id: pid3 ? v(pid3['PID.3.1']) : null,
  patient_sex: v(msg['PID']['PID.8']['PID.8.1']),
  birth_date: v(msg['PID']['PID.7']['PID.7.1']),
  event_datetime: v(msg['EVN']['EVN.2']['EVN.2.1']) || v(msg['MSH']['MSH.7']['MSH.7.1']),
  project: java.lang.System.getenv('SCOUT_PROJECT'),
  diagnoses: [],
  orders: [],
  observations: []
};
for each (var dg1 in msg..DG1) {
  ev.diagnoses.push({
    code: v(dg1['DG1.3']['DG1.3.1']),
    text: v(dg1['DG1.3']['DG1.3.2']) || v(dg1['DG1.4']['DG1.4.1']),
    system: v(dg1['DG1.3']['DG1.3.3']) || v(dg1['DG1.2']['DG1.2.1'])
  });
}
if (type == 'ORM^O01') {
  for each (var obr in msg..OBR) {
    ev.orders.push({code: v(obr['OBR.4']['OBR.4.1']), text: v(obr['OBR.4']['OBR.4.2'])});
  }
}
for each (var obx in msg..OBX) {
  ev.observations.push({
    code: v(obx['OBX.3']['OBX.3.1']),
    name: v(obx['OBX.3']['OBX.3.2']),
    value: v(obx['OBX.5']['OBX.5.1']),
    units: v(obx['OBX.6']['OBX.6.1']),
    flag: v(obx['OBX.8']['OBX.8.1'])
  });
}

// Client-credentials token for the indexer (cached in the global map until expiry).
function jwtExpiryMillis(jwt) {
  // Trust the token's own exp claim (server clock), not a locally computed expiry.
  var part = String(jwt).split('.')[1];
  var json = new java.lang.String(java.util.Base64.getUrlDecoder().decode(part), 'UTF-8');
  return JSON.parse(String(json)).exp * 1000;
}
function scoutToken() {
  var now = new Date().getTime();
  var cached = globalMap.get('scout_token');
  if (cached && jwtExpiryMillis(cached) - now > 60000) return String(cached);
  function env(name) { return String(java.lang.System.getenv(name)); }
  // Apache HttpClient ships with Mirth; java.net.HttpURLConnection is not reachable from
  // Rhino on Java 17 (module encapsulation).
  var http = org.apache.http;
  var client = http.impl.client.HttpClients.createDefault();
  var post = new http.client.methods.HttpPost(env('KC_TOKEN_URL'));
  var form = new java.util.ArrayList();
  form.add(new http.message.BasicNameValuePair('grant_type', 'client_credentials'));
  form.add(new http.message.BasicNameValuePair('client_id', env('KC_MIRTH_CLIENT_ID')));
  form.add(new http.message.BasicNameValuePair('client_secret', env('KC_MIRTH_CLIENT_SECRET')));
  post.setEntity(new http.client.entity.UrlEncodedFormEntity(form, 'UTF-8'));
  var text;
  try {
    var resp = client.execute(post);
    text = String(http.util.EntityUtils.toString(resp.getEntity(), 'UTF-8'));
    if (resp.getStatusLine().getStatusCode() != 200) throw 'token endpoint returned ' + resp.getStatusLine().getStatusCode();
  } finally {
    client.close();
  }
  var token = JSON.parse(text);
  globalMap.put('scout_token', token.access_token);
  return token.access_token;
}

// W3C trace context so the indexer's spans join a trace started here.
function hex(bytes) {
  var s = '';
  for (var i = 0; i < bytes; i++) s += ('0' + Math.floor(Math.random() * 256).toString(16)).slice(-2);
  return s;
}

channelMap.put('payload', JSON.stringify({events: [ev]}));
channelMap.put('token', scoutToken());
channelMap.put('indexer_url', java.lang.System.getenv('INDEXER_URL') + '/clinical/events');
channelMap.put('traceparent', '00-' + hex(16) + '-' + hex(8) + '-01');
channelMap.put('message_type', type);
