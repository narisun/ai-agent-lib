## Covenant Breaches

Both covenants on facility FAC000000130 are not in compliance [covenants[facility_id=FAC000000130;name=DSCR], covenants[facility_id=FAC000000130;name=Fixed Charge Coverage]]:

- **DSCR:** Current value 1.08 versus threshold ≥1.25 (shortfall of 0.17)
- **Fixed Charge Coverage:** Current value 0.96 versus threshold ≥1.15 (shortfall of 0.19)

The covenant test observation date is not supplied; next test date 2026-09-28 is a future milestone, not the test date. Ratio methodology is not supplied, so the calculation methods are unknown.

Two open credit exceptions address these breaches. Exception CE-JONE-001 requires execution of a covenant waiver and approval of the liquidity remediation plan; it is 26 days overdue from due date 2026-07-08. A recorded waiver_expiry of 2026-08-15 exists, but execution is not established by this source [credit_exceptions[exception_id=CE-JONE-001]]. The recorded cure plan specifies weekly liquidity reporting and no additional debt without approval, but which conditions apply if a waiver was executed is unknown.

---

## Cash Conversion Cycle Change

The cash conversion cycle increased by 19.00 days (21.84% change) from 87 days at 2026-03-31 to 106 days at 2026-06-30 [working_capital[uen=JONE-0022;period_end=2026-03-31], working_capital[uen=JONE-0022;period_end=2026-06-30]].

**Components of the 19-day increase:**
- **DSO increased 11.00 days** (16.92%) from 65 to 76 days
- **DIO increased 7.00 days** (11.86%) from 59 to 66 days  
- **DPO decreased 1.00 day** (-2.70%) from 37 to 36 days

AR over 90 days increased 9.00 percentage points to 23.0%; inventory aging increased 8.00 percentage points to 22.0%. Net working capital decreased $7.0M (-53.85%) to $6.0M USD [working_capital[uen=JONE-0022;period_end=2026-03-31], working_capital[uen=JONE-0022;period_end=2026-06-30]].

Causes of these metric changes are not established by the working-capital values. Possible explanations to investigate: Did collections slow? What drove the increase in aged receivables? What explains the net working capital decline? Was there associated cash flow, and if so, how was cash used?

---

## Wallet Share Calculation

Wallet share is recorded as 40.21% [relationship[uen=JONE-0022]]. This is a supplied relationship metric, not a calculation performed from the evidence.

The numerator, denominator, measurement period, and methodology are not provided in the evidence. I cannot determine how this percentage was calculated from the available deposits ($4.89M total), exposure ($47.78M), bank-attributed income (NII $430K, NIR $169K), or payment flows. These are different measures that do not establish the wallet-share calculation basis.