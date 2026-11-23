# Verified Hong Kong general holidays

Verified 2026-11-21 using direct web reads of the HKSAR Government releases:

- 2026: https://www.info.gov.hk/gia/general/202505/16/P2025051300353.htm (published 2025-05-16, full holiday table lines 19–36).
- 2027: https://www.info.gov.hk/gia/general/202605/15/P2026051400300p.htm (published 2026-05-15, full holiday table lines 7–24).

The JSON contains all 17 named general-holiday dates per year, including Saturdays. Saturdays and Sundays are excluded independently. These are general/public holidays, not the smaller labour statutory-holiday list. No mainland working-weekend adjustments apply. Dates and reminder slots use Hong Kong UTC+8.

2026 includes the special April 6/7 consecutive substituted holidays, May 25 and October 19 substitutions. 2027 includes February 9 substitution and December 27. Official releases explicitly explain their substitution rules. Sunday holidays need no duplicate JSON entry because every Sunday is excluded. Missing years fail with `CalendarUnavailable` (pending verification), rather than silently assuming weekdays are business days.

Gov.hk summary pages returned no readable body and local curl DNS failed. The info.gov.hk government releases above were successfully opened and their complete tables read through the web tool. JSON hashes cover normalized date lists, not claimed raw HTML bytes. Data is factual transcription, not a fake fixture or a claim to have downloaded original HTML.
