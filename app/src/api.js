// Every call goes through here so error handling lives in one place.
async function request(path, options = {}) {
  const response = await fetch(path, options)
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = (await response.json()).detail ?? detail
    } catch {
      // response had no JSON body; the status text will do
    }
    throw new Error(detail)
  }
  return response.json()
}

export const api = {
  overview: () => request('/api/overview'),
  lineage: () => request('/api/lineage'),
  incidents: () => request('/api/incidents'),
  incident: (root, explain = false) =>
    request(`/api/incidents/${encodeURIComponent(root)}?explain=${explain}`),
  metrics: (model, column, metric = 'mean_value') =>
    request(`/api/metrics/${encodeURIComponent(model)}/${encodeURIComponent(column)}?metric=${metric}`),
  faults: () => request('/api/faults'),
  injectFault: (key) => request(`/api/faults/${key}`, { method: 'POST' }),
  reset: () => request('/api/reset', { method: 'POST' }),
}