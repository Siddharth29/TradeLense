import { http, HttpResponse } from 'msw'

export const handlers = [
  http.get('/agents', () =>
    HttpResponse.json({
      agents: [
        {
          id: 'AAPL',
          symbol: 'AAPL',
          candle_count_5m: 1,
          candle_count_15m: 1,
          broker: 'mt5',
          risk_percent: 1,
          run_interval: 'hourly',
          prompt_id: 'prompt-1',
        },
      ],
    }),
  ),
  http.get('/metrics/agents', () =>
    HttpResponse.json({
      AAPL: {
        agent_id: 'AAPL',
        pnl: 100,
        run_interval: 'hourly',
        risk_percent: 1,
        runs: 0,
        decisions: 0,
        orders: 0,
        wins: 0,
        losses: 0,
        budget_skips: 0,
        latency_p50: null,
        latency_p95: null,
        drift_p50: null,
        drift_p95: null,
        hit_rate: null,
        last_run: null,
        eta: null,
        summary: '',
        running: false,
        usage: null,
      },
    })
  ),
  http.get('/agents/:asset/status', ({ params }) =>
    HttpResponse.json({
      running: false,
      run_interval: 'hourly',
      risk_percent: 1,
      asset: params.asset,
    })
  ),
  http.post('/configs/brokers', () => HttpResponse.json({ status: 'ok' })),
  http.get('/configs/gpt', () =>
    HttpResponse.json({
      api_key: '',
      model: 'gpt',
      temperature: 1,
      assistant_id: 'aid-1',
    }),
  ),
  http.put('/configs/gpt', () => HttpResponse.json({ status: 'ok' })),
  http.post('/agents/:asset/start', ({ params }) =>
    HttpResponse.json({ status: `started ${params.asset}` })
  ),
  http.post('/agents/:asset/stop', ({ params }) =>
    HttpResponse.json({ status: `stopped ${params.asset}` })
  ),
  http.put('/agents/:id', async ({ request }) => {
    const body = await request.json()
    return HttpResponse.json(body)
  }),
  http.get('/journal/list', ({ request }) => {
    const url = new URL(request.url)
    const asset = url.searchParams.get('asset') ?? ''
    return HttpResponse.json({ entries: asset ? [{ id: 1, asset }] : [] })
  }),
  http.get('/configs/:domain/versions', () =>
    HttpResponse.json({ versions: [{ version: 2, doc: {} }] })
  ),
  http.post('/configs/:domain/rollback', () =>
    HttpResponse.json({ status: 'ok' })
  ),
  http.get('/configs/flags', () =>
    HttpResponse.json({ enable_backtest: false, enable_stream: false })
  ),
  http.put('/configs/flags', () => HttpResponse.json({ status: 'ok' })),
  http.get('/brokers/mt5/status', () => HttpResponse.json({ connected: false })),
  http.get('/brokers/mt5/connect', () => HttpResponse.json({ status: 'ok' })),
]
