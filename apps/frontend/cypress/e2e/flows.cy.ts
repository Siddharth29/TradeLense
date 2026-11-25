describe('API flows', () => {
  beforeEach(() => {
    cy.visit('about:blank')
  })

  it('saves broker config', () => {
    cy.intercept('POST', '/configs/brokers', { status: 'ok' }).as('save')
    cy.window().then((w) => w.fetch('/configs/brokers', { method: 'POST' }))
    cy.wait('@save').its('response.body.status').should('eq', 'ok')
  })

  it('starts and stops agent', () => {
    cy.intercept('POST', '/agents/TEST/start', { status: 'started' }).as('start')
    cy.intercept('POST', '/agents/TEST/stop', { status: 'stopped' }).as('stop')
    cy.window().then((w) => w.fetch('/agents/TEST/start', { method: 'POST' }))
    cy.wait('@start').its('response.body.status').should('eq', 'started')
    cy.window().then((w) => w.fetch('/agents/TEST/stop', { method: 'POST' }))
    cy.wait('@stop').its('response.body.status').should('eq', 'stopped')
  })

  it('receives event stream', () => {
    cy.intercept('/events', (req) => {
      req.reply('data: {"asset":"AAPL","type":"info"}\n\n', {
        headers: { 'Content-Type': 'text/event-stream' },
      })
    }).as('events')
    cy.window().then((w) => {
      const messages: string[] = []
      const es = new w.EventSource('/events')
      es.onmessage = (e) => {
        messages.push(e.data)
        es.close()
      }
      ;(w as any).msgs = messages
    })
    cy.wait('@events')
    cy.window().its('msgs').should('deep.equal', ['{"asset":"AAPL","type":"info"}'])
  })

  it('filters journal', () => {
    cy.intercept('GET', '/journal/list*', (req) => {
      const url = new URL(req.url)
      expect(url.searchParams.get('asset')).to.eq('AAPL')
      req.reply({ entries: [{ asset: 'AAPL' }] })
    }).as('journal')
    cy.window().then((w) => w.fetch('/journal/list?asset=AAPL'))
    cy.wait('@journal').its('response.body.entries[0].asset').should('eq', 'AAPL')
  })
})
