import { describe, expect, it } from 'vitest'
import { resolveOption } from './optionsResolver'

const asDate = (y: number, m: number, d: number) => new Date(Date.UTC(y, m - 1, d))

describe('options resolver', () => {
  it('nifty atm current week round up', () => {
    const inst = resolveOption(
      'NIFTY',
      'CE',
      'ATM',
      'current_week',
      17625,
      asDate(2024, 1, 15),
    )
    expect(inst.strike).toBe(17650)
    expect(inst.expiry.toISOString()).toBe(asDate(2024, 1, 18).toISOString())
    expect(inst.optionType).toBe('CE')
    expect(inst.lotSize).toBe(50)
    expect(inst.tickSize).toBe(0.05)
    expect(inst.tradingsymbol.startsWith('NIFTY')).toBe(true)
  })

  it('nifty put otm monthly', () => {
    const inst = resolveOption(
      'NIFTY',
      'PE',
      'OTM_2',
      'current_month',
      18012,
      asDate(2024, 1, 10),
    )
    expect(inst.strike).toBe(17900)
    expect(inst.expiry.toISOString()).toBe(asDate(2024, 1, 25).toISOString())
    expect(inst.optionType).toBe('PE')
  })

  it('banknifty call itm next week', () => {
    const inst = resolveOption(
      'BANKNIFTY',
      'CE',
      'ITM_1',
      'next_week',
      48250,
      asDate(2024, 1, 15),
    )
    expect(inst.strike).toBe(48200)
    expect(inst.expiry.toISOString()).toBe(asDate(2024, 1, 25).toISOString())
    expect(inst.lotSize).toBe(15)
  })

  it('banknifty put atm next month', () => {
    const inst = resolveOption(
      'BANKNIFTY',
      'PE',
      'ATM',
      'next_month',
      41049,
      asDate(2024, 1, 29),
    )
    expect(inst.strike).toBe(41000)
    expect(inst.expiry.toISOString()).toBe(asDate(2024, 2, 29).toISOString())
  })

  it('edge rounding behavior', () => {
    expect(
      resolveOption('NIFTY', 'CE', 'ATM', 'current_week', 17624, asDate(2024, 1, 15)).strike,
    ).toBe(17600)
    expect(
      resolveOption('NIFTY', 'CE', 'ATM', 'current_week', 17625, asDate(2024, 1, 15)).strike,
    ).toBe(17650)
    expect(
      resolveOption(
        'BANKNIFTY',
        'CE',
        'ATM',
        'current_week',
        44149,
        asDate(2024, 1, 15),
      ).strike,
    ).toBe(44100)
    expect(
      resolveOption(
        'BANKNIFTY',
        'CE',
        'ATM',
        'current_week',
        44150,
        asDate(2024, 1, 15),
      ).strike,
    ).toBe(44200)
  })
})
