"""
Patches the upgradeRiskPredictions function in backend.mjs to redirect URP calls
from console.redhat.com to the on-prem service.

The console backend hardcodes console.redhat.com for upgrade risk predictions:
https://github.com/stolostron/console/blob/25e89cf074e27ef24bc850778123e281a767d9ab/backend/src/routes/upgrade-risks-prediction.ts#L55

This patch replaces that function so each cluster ID is sent to the on-prem
/upgrade-risks-prediction endpoint instead, and the response is formatted to match
the ccx-upgrades-data-eng MultiClusterUpgradeApiResponse shape that the frontend expects:
{ statusCode: 200, body: { predictions: [...] } }
"""
import re
import sys

PATCHED_MARKER = 'insights-on-prem.insights-on-prem-poc.svc.cluster.local'

PATCHED_FN = '''async function upgradeRiskPredictions(req, res) {
    const token = await getAuthenticatedToken(req, res);
    if (token) {
        const chucks = [];
        req.on('data', (chuck) => { chucks.push(chuck); });
        req.on('end', async () => {
            try {
                const body = JSON.parse(chucks.join());
                const onPremBase = 'http://insights-on-prem.insights-on-prem-poc.svc.cluster.local:8000';
                const predictions = await Promise.all(
                    body.clusterIds.map((clusterId) =>
                        fetch(onPremBase + '/upgrade-risks-prediction', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ cluster_id: clusterId })
                        })
                        .then((r) => r.json())
                        .then((result) => ({
                            cluster_id: clusterId,
                            prediction_status: 'ok',
                            upgrade_recommended: result.upgrade_recommended,
                            upgrade_risks_predictors: result.upgrade_risks_predictors,
                            last_checked_at: new Date().toISOString()
                        }))
                        .catch(() => ({
                            cluster_id: clusterId,
                            prediction_status: 'No data for the cluster'
                        }))
                    )
                );
                res.setHeader('Content-Type', 'application/json');
                res.end(JSON.stringify([{ statusCode: 200, body: { predictions: predictions } }]));
            } catch (err) {
                logger.error(err);
                respondInternalServerError(req, res);
            }
        });
    }
}'''

with open('/tmp/backend.mjs', 'r') as f:
    content = f.read()

if PATCHED_MARKER in content:
    print("backend.mjs already patched, reusing")
    sys.exit(0)

new_content = re.sub(
    r'async function upgradeRiskPredictions\(req, res\) \{.*?\n\}',
    PATCHED_FN, content, flags=re.DOTALL
)

if new_content == content:
    print("ERROR: could not patch upgradeRiskPredictions - function not found", file=sys.stderr)
    sys.exit(1)

with open('/tmp/backend.mjs', 'w') as f:
    f.write(new_content)

print("backend.mjs patched")
