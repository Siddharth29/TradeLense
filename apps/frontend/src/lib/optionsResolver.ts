export interface OptionInstrument {
  tradingsymbol: string;
  token: string;
  strike: number;
  optionType: 'CE' | 'PE';
  expiry: Date;
  lotSize: number;
  tickSize: number;
}

const OPTION_META: Record<string, { step: number; lot: number; tick: number }> = {
  NIFTY: { step: 50, lot: 50, tick: 0.05 },
  BANKNIFTY: { step: 100, lot: 15, tick: 0.05 },
};

function roundStrike(spot: number, step: number): number {
  return Math.floor((spot + step / 2) / step) * step;
}

function nextThursday(d: Date): Date {
  const result = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const diff = (4 - result.getUTCDay() + 7) % 7;
  result.setUTCDate(result.getUTCDate() + diff);
  return result;
}

function lastThursday(year: number, month: number): Date {
  const nextMonth =
    month === 12
      ? new Date(Date.UTC(year + 1, 0, 1))
      : new Date(Date.UTC(year, month, 1));
  const lastDay = new Date(nextMonth.getTime() - 24 * 60 * 60 * 1000);
  const diff = (lastDay.getUTCDay() - 4 + 7) % 7;
  lastDay.setUTCDate(lastDay.getUTCDate() - diff);
  return lastDay;
}

export function resolveOption(
  indexSymbol: string,
  direction: 'CE' | 'PE',
  strikeRule: string,
  expiryRule: string,
  spotPrice: number,
  asOf: Date,
): OptionInstrument {
  const meta = OPTION_META[indexSymbol.toUpperCase()];
  if (!meta) throw new Error('unsupported index symbol');
  const step = meta.step;

  const atmStrike = roundStrike(spotPrice, step);
  const optionType = direction.toUpperCase() as 'CE' | 'PE';
  let strike = atmStrike;

  const rule = strikeRule.toUpperCase();
  if (rule !== 'ATM') {
    const parts = rule.split('_');
    if (parts.length !== 2) throw new Error('invalid strike_rule');
    const kind = parts[0];
    const k = parseInt(parts[1], 10);
    if (Number.isNaN(k)) throw new Error('invalid strike_rule');
    let offset = 0;
    if (kind === 'OTM') {
      offset = optionType === 'CE' ? k : -k;
    } else if (kind === 'ITM') {
      offset = optionType === 'CE' ? -k : k;
    } else {
      throw new Error('invalid strike_rule');
    }
    strike = atmStrike + offset * step;
  }

  const erule = expiryRule.toLowerCase();
  let expiry: Date;
  if (erule === 'current_week') {
    expiry = nextThursday(asOf);
  } else if (erule === 'next_week') {
    expiry = nextThursday(asOf);
    expiry.setDate(expiry.getDate() + 7);
  } else if (erule === 'current_month') {
    expiry = lastThursday(asOf.getFullYear(), asOf.getMonth() + 1);
    if (asOf > expiry) {
      let month = asOf.getMonth() + 2; // next month 1-based
      let year = asOf.getFullYear();
      if (month > 12) {
        year += 1;
        month = 1;
      }
      expiry = lastThursday(year, month);
    }
  } else if (erule === 'next_month') {
    let month = asOf.getMonth() + 2; // next month 1-based
    let year = asOf.getFullYear();
    if (month > 12) {
      year += 1;
      month = 1;
    }
    expiry = lastThursday(year, month);
  } else {
    throw new Error('invalid expiry_rule');
  }

  const yy = (expiry.getFullYear() % 100).toString().padStart(2, '0');
  const mm = (expiry.getMonth() + 1).toString().padStart(2, '0');
  const dd = expiry.getDate().toString().padStart(2, '0');
  const tradingsymbol = `${indexSymbol.toUpperCase()}${yy}${mm}${dd}${Math.trunc(strike)}${optionType}`;
  const token = `${indexSymbol.toUpperCase()}_${Math.trunc(strike)}_${optionType}_${expiry.getFullYear()}${mm}${dd}`;

  return {
    tradingsymbol,
    token,
    strike: Math.trunc(strike),
    optionType,
    expiry,
    lotSize: meta.lot,
    tickSize: meta.tick,
  };
}

export { OPTION_META };
