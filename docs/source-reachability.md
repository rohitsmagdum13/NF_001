# Source Reachability Report

Generated: 2026-04-24 11:41
Source template: `reference001/reference/Water_Newsfeed_Template.md`
URLs tested: **79** (deduplicated)
Method: `httpx.get()` with Chrome 120 User-Agent, 12s timeout, follow-redirects.

## Summary

| Tier | Count | Meaning | Recommended handling |
|---|---|---|---|
| OK | 42 | HTTP 200-399 | Direct fetch via httpx. Add to sources.yaml without `requires_js`. |
| BLOCKED (403) | 10 | WAF / bot protection | Add to sources.yaml with `requires_js: true` (Playwright). |
| TIMEOUT | 14 | Slow or firewalled | Try Playwright; may need corporate proxy bypass. |
| 404 | 6 | Wrong or dead path | Skip, or manually find the replacement URL. |
| ERR:ConnectError | 7 | Network/HTTP error | Investigate case-by-case. |

## Directly-usable URLs (42 total)

Copy these into `data/local_articles/sources.yaml` under the matching
`(section, jurisdiction)` group. No `requires_js` flag needed — httpx handles them.

### 1. Water Reform / National

- https://www.accc.gov.au/focus-areas/inquiries-ongoing/murray-darling-basin-water-markets-inquiry
- https://www.wsaa.asn.au/news
- https://watersource.awa.asn.au/
- http://www.awa.asn.au/AWA_MBRR/Publications/AWA_MBRR/Publications/Publications_and_Information.aspx?hkey=ca21efb0-39e3-4274-b1a4-0f500472a26a
- https://www.mdba.gov.au/media/media-releases?date[value
- https://www.mdba.gov.au/river-information/weekly-reports
- http://nban.org.au/
- http://www.bom.gov.au/water/?ref=ftr
- https://www.waterra.com.au/
- http://www.abc.net.au/news/topic/water
- https://www.theguardian.com/uk/environment
- https://watersensitivecities.org.au/content_type/news/

### 1. Water Reform / New South Wales

- http://www.waternsw.com.au/about/newsroom
- https://www.industry.nsw.gov.au/water/allocations-availability/allocations/determinations
- https://www.industry.nsw.gov.au/water/allocations-availability/allocations/statements
- https://www.industry.nsw.gov.au/water/news
- https://www.mirrigation.com.au/

### 1. Water Reform / Queensland

- http://www.qldwater.com.au/qldwater-blog
- https://www.sunwater.com.au/news/
- http://seqwater.com.au/media/media-releases/
- http://seqwater.com.au/media
- https://www.urbanutilities.com.au/newsroom

### 1. Water Reform / South Australia

- http://www.sawater.com.au/about-us/media-centre
- https://www.waterconnect.sa.gov.au/River-Murray/SitePages/2020%20Flow%20Reports.aspx

### 1. Water Reform / Tasmania

- http://www.taswater.com.au/News/TasWater-News/TasWater-News
- http://www.economicregulator.tas.gov.au/about-us/media-releases
- https://www.tasmanianirrigation.com.au/

### 1. Water Reform / Victoria

- https://www.melbournewater.com.au/
- https://www.melbournewater.com.au/water/water-storage-and-use#/ws/freq/weekly/type/storage
- https://www.esc.vic.gov.au/media-centre
- http://southeastwater.com.au/NewsEvents/Newsroom/Pages/News.aspx
- http://www.g-mwater.com.au/
- http://www.gwmwater.org.au/news
- http://www.srw.com.au/media/
- http://www.sustainability.vic.gov.au/
- https://www.ewov.com.au/
- http://waterregister.vic.gov.au/
- https://www.yvw.com.au/about-us/news

### 1. Water Reform / Western Australia

- http://water.wa.gov.au/news/current-news
- http://www.water.wa.gov.au/search-publications
- https://www.watercorporation.com.au/
- https://www.der.wa.gov.au/about-us/media-statements

## URLs that need Playwright (24 total)

Add these to sources.yaml with `requires_js: true` — Stage 1/4 will route via Playwright.

### 1. Water Reform / National

- `TIMEOUT` — https://www.environment.gov.au/water/cewo/media-release
- `TIMEOUT` — https://www.nationalwatergrid.gov.au/
- `TIMEOUT` — https://www.agriculture.gov.au/abares/publications
- `TIMEOUT` — http://www.agriculture.gov.au/about/media-centre
- `TIMEOUT` — https://www.environment.gov.au/news/commonwealth-environmental-water-office
- `TIMEOUT` — http://www.agriculture.gov.au/abares/publications/weekly_update
- `TIMEOUT` — http://lakeeyrebasin.gov.au/
- `TIMEOUT` — https://www.waterquality.gov.au/
- `TIMEOUT` — http://epbcnotices.environment.gov.au/publicnoticesreferrals/

### 1. Water Reform / New South Wales

- `TIMEOUT` — https://www.hunterwater.com.au/
- `TIMEOUT` — https://www.ipart.nsw.gov.au/Home/Media-Centre

### 1. Water Reform / Northern Territory

- `BLOCKED` — http://mediareleases.nt.gov.au/
- `BLOCKED` — https://denr.nt.gov.au/news
- `BLOCKED` — https://www.powerwater.com.au/about/news-and-media
- `BLOCKED` — https://utilicom.nt.gov.au/news

### 1. Water Reform / Queensland

- `BLOCKED` — https://www.dnrm.qld.gov.au/our-department/news

### 1. Water Reform / South Australia

- `TIMEOUT` — https://www.environment.sa.gov.au/news-hub
- `BLOCKED` — http://www.escosa.sa.gov.au/news

### 1. Water Reform / Victoria

- `TIMEOUT` — https://www.citywestwater.com.au/news_centre/media_centre.aspx
- `BLOCKED` — https://www.lmw.vic.gov.au/news-media/
- `TIMEOUT` — https://www.gippswater.com.au/
- `BLOCKED` — https://www.barwonwater.vic.gov.au/about-us/news-and-events
- `BLOCKED` — http://www.vewh.vic.gov.au/news-and-publications/news

### 9. General Water Quality and Sustainability / International

- `BLOCKED` — http://www.unwater.org/

## Full reachability matrix

### 1. Water Reform / National

| Tier | Status | URL | Final URL (after redirects) |
|---|---|---|---|
| OK | 200 | https://www.accc.gov.au/focus-areas/inquiries-ongoing/murray-darling-basin-water-markets-inquiry | https://www.accc.gov.au/inquiries-and-consultations/finalised-inquiries/murray-darling-basin-water-markets-inquiry-2019-21 *(redirected)* |
| OK | 200 | https://www.wsaa.asn.au/news | https://www.wsaa.asn.au/news |
| OK | 200 | https://watersource.awa.asn.au/ | https://www.awa.asn.au/ *(redirected)* |
| OK | 200 | http://www.awa.asn.au/AWA_MBRR/Publications/AWA_MBRR/Publications/Publications_and_Information.aspx?hkey=ca21efb0-39e3-4274-b1a4-0f500472a26a | https://www.awa.asn.au/resources?hkey=ca21efb0-39e3-4274-b1a4-0f500472a26a *(redirected)* |
| TIMEOUT | 0 | https://www.environment.gov.au/water/cewo/media-release |  |
| TIMEOUT | 0 | https://www.nationalwatergrid.gov.au/ |  |
| ERR:ConnectError | 0 | https://www.igmdb.gov.au/reviews |  |
| TIMEOUT | 0 | https://www.agriculture.gov.au/abares/publications |  |
| TIMEOUT | 0 | http://www.agriculture.gov.au/about/media-centre |  |
| OK | 200 | https://www.mdba.gov.au/media/media-releases?date[value | https://webarchive.nla.gov.au/awa/20230626101836/https://www.mdba.gov.au/news-media-events/newsroom/media-centre?date_value= *(redirected)* |
| 404 | 404 | https://www.mdba.gov.au/publications/all-publications | https://www.mdba.gov.au/publications/all-publications |
| OK | 200 | https://www.mdba.gov.au/river-information/weekly-reports | https://www.mdba.gov.au/publications-and-data/data-and-dashboards/river-murray-weekly-reports-0 *(redirected)* |
| TIMEOUT | 0 | https://www.environment.gov.au/news/commonwealth-environmental-water-office |  |
| ERR:ConnectError | 0 | https://minister.awe.gov.au/ |  |
| ERR:ConnectError | 0 | https://minister.awe.gov.au/littleproud |  |
| TIMEOUT | 0 | http://www.agriculture.gov.au/abares/publications/weekly_update |  |
| TIMEOUT | 0 | http://lakeeyrebasin.gov.au/ |  |
| ERR:ConnectError | 0 | http://gabcc.org.au/12094/GAB-News/ |  |
| OK | 200 | http://nban.org.au/ | https://nban.org.au/ *(redirected)* |
| OK | 200 | http://www.bom.gov.au/water/?ref=ftr | https://www.bom.gov.au/water/?ref=ftr *(redirected)* |
| ERR:ConnectError | 0 | https://www.mdbrc.sa.gov.au/ |  |
| OK | 200 | https://www.waterra.com.au/ | https://www.waterra.com.au/ |
| OK | 200 | http://www.abc.net.au/news/topic/water | https://www.abc.net.au/news/topic/water-resources?future=true *(redirected)* |
| OK | 200 | https://www.theguardian.com/uk/environment | https://www.theguardian.com/uk/environment |
| OK | 200 | https://watersensitivecities.org.au/content_type/news/ | https://watersensitivecities.org.au/content_type/news/ |
| TIMEOUT | 0 | https://www.waterquality.gov.au/ |  |
| TIMEOUT | 0 | http://epbcnotices.environment.gov.au/publicnoticesreferrals/ |  |

### 1. Water Reform / New South Wales

| Tier | Status | URL | Final URL (after redirects) |
|---|---|---|---|
| OK | 200 | http://www.waternsw.com.au/about/newsroom | https://www.waternsw.com.au/community-news/media-releases *(redirected)* |
| OK | 200 | https://www.industry.nsw.gov.au/water/allocations-availability/allocations/determinations | https://www.nsw.gov.au/departments-and-agencies/department-of-planning-housing-and-infrastructure *(redirected)* |
| OK | 200 | https://www.industry.nsw.gov.au/water/allocations-availability/allocations/statements | https://www.nsw.gov.au/departments-and-agencies/department-of-planning-housing-and-infrastructure *(redirected)* |
| 404 | 404 | http://www.sydneywater.com.au/SW/about-us/our-publications/Media/index.htm | https://www.sydneywater.com.au/SW/about-us/our-publications/Media/index.htm *(redirected)* |
| TIMEOUT | 0 | https://www.hunterwater.com.au/ |  |
| OK | 200 | https://www.industry.nsw.gov.au/water/news | https://www.nsw.gov.au/departments-and-agencies/department-of-planning-housing-and-infrastructure *(redirected)* |
| OK | 200 | https://www.mirrigation.com.au/ | https://www.mirrigation.com.au/ |
| TIMEOUT | 0 | https://www.ipart.nsw.gov.au/Home/Media-Centre |  |

### 1. Water Reform / Northern Territory

| Tier | Status | URL | Final URL (after redirects) |
|---|---|---|---|
| BLOCKED | 403 | http://mediareleases.nt.gov.au/ | https://mediareleases.nt.gov.au/ *(redirected)* |
| BLOCKED | 403 | https://denr.nt.gov.au/news | https://denr.nt.gov.au/news |
| BLOCKED | 403 | https://www.powerwater.com.au/about/news-and-media | https://www.powerwater.com.au/about/news-and-media |
| BLOCKED | 403 | https://utilicom.nt.gov.au/news | https://utilicom.nt.gov.au/news |

### 1. Water Reform / Queensland

| Tier | Status | URL | Final URL (after redirects) |
|---|---|---|---|
| OK | 200 | http://www.qldwater.com.au/qldwater-blog | https://qldwater.com.au/qldwater-blog *(redirected)* |
| 404 | 404 | https://www.qldwater.com.au/e-flashes-2020 | https://qldwater.com.au/e-flashes-2020 *(redirected)* |
| BLOCKED | 403 | https://www.dnrm.qld.gov.au/our-department/news | https://www.dnrm.qld.gov.au/our-department/news |
| OK | 200 | https://www.sunwater.com.au/news/ | https://www.sunwater.com.au/news-and-alerts/ *(redirected)* |
| OK | 200 | http://seqwater.com.au/media/media-releases/ | https://www.seqwater.com.au/news *(redirected)* |
| OK | 200 | http://seqwater.com.au/media | https://www.seqwater.com.au/news *(redirected)* |
| OK | 200 | https://www.urbanutilities.com.au/newsroom | https://www.urbanutilities.com.au/newsroom |

### 1. Water Reform / South Australia

| Tier | Status | URL | Final URL (after redirects) |
|---|---|---|---|
| TIMEOUT | 0 | https://www.environment.sa.gov.au/news-hub |  |
| OK | 200 | http://www.sawater.com.au/about-us/media-centre | https://www.sawater.com.au/about-us/media/media-centre *(redirected)* |
| BLOCKED | 403 | http://www.escosa.sa.gov.au/news | https://www.escosa.sa.gov.au/news *(redirected)* |
| OK | 200 | https://www.waterconnect.sa.gov.au/River-Murray/SitePages/2020%20Flow%20Reports.aspx | https://www.waterconnect.sa.gov.au/River-Murray/SitePages/2020%20Flow%20Reports.aspx |

### 1. Water Reform / Tasmania

| Tier | Status | URL | Final URL (after redirects) |
|---|---|---|---|
| 404 | 404 | https://dpipwe.tas.gov.au/hot-topics | https://nre.tas.gov.au/hot-topics *(redirected)* |
| OK | 200 | http://www.taswater.com.au/News/TasWater-News/TasWater-News | https://www.taswater.com.au/news/newsroom *(redirected)* |
| 404 | 404 | http://www.taswater.com.au/News/Media-Releases | https://www.taswater.com.au/News/Media-Releases *(redirected)* |
| OK | 200 | http://www.economicregulator.tas.gov.au/about-us/media-releases | https://www.economicregulator.tas.gov.au/about-us/media-releases *(redirected)* |
| OK | 200 | https://www.tasmanianirrigation.com.au/ | https://tasmanianirrigation.com.au/ *(redirected)* |

### 1. Water Reform / Victoria

| Tier | Status | URL | Final URL (after redirects) |
|---|---|---|---|
| ERR:ConnectError | 0 | https://www2.delwp.vic.gov.au/media-centre/news-and-announcements |  |
| OK | 200 | https://www.melbournewater.com.au/ | https://www.melbournewater.com.au/ |
| OK | 200 | https://www.melbournewater.com.au/water/water-storage-and-use#/ws/freq/weekly/type/storage | https://www.melbournewater.com.au/water-and-environment/water-management/water-storage-levels#/ws/freq/weekly/type/storage *(redirected)* |
| OK | 200 | https://www.esc.vic.gov.au/media-centre | https://www.esc.vic.gov.au/media-centre |
| TIMEOUT | 0 | https://www.citywestwater.com.au/news_centre/media_centre.aspx |  |
| BLOCKED | 403 | https://www.lmw.vic.gov.au/news-media/ | https://www.lmw.vic.gov.au/news-media/ |
| OK | 200 | http://southeastwater.com.au/NewsEvents/Newsroom/Pages/News.aspx | https://southeastwater.com.au/ *(redirected)* |
| OK | 200 | http://www.g-mwater.com.au/ | https://www.g-mwater.com.au/ *(redirected)* |
| TIMEOUT | 0 | https://www.gippswater.com.au/ |  |
| OK | 200 | http://www.gwmwater.org.au/news | https://www.gwmwater.org.au/news *(redirected)* |
| OK | 200 | http://www.srw.com.au/media/ | https://www.srw.com.au/news-media *(redirected)* |
| OK | 200 | http://www.sustainability.vic.gov.au/ | https://www.sustainability.vic.gov.au/ *(redirected)* |
| 404 | 404 | http://www.ccma.vic.gov.au/Home.aspx | http://ccma.vic.gov.au/Home.aspx *(redirected)* |
| ERR:ConnectError | 0 | http://www.malleecma.vic.gov.au/ |  |
| OK | 200 | https://www.ewov.com.au/ | https://www.ewov.com.au/ |
| BLOCKED | 403 | https://www.barwonwater.vic.gov.au/about-us/news-and-events | https://www.barwonwater.vic.gov.au/about-us/news-and-events |
| BLOCKED | 403 | http://www.vewh.vic.gov.au/news-and-publications/news | https://www.vewh.vic.gov.au/news-and-publications/news *(redirected)* |
| OK | 200 | http://waterregister.vic.gov.au/ | https://waterregister.vic.gov.au/ *(redirected)* |
| OK | 200 | https://www.yvw.com.au/about-us/news | https://www.yvw.com.au/news *(redirected)* |

### 1. Water Reform / Western Australia

| Tier | Status | URL | Final URL (after redirects) |
|---|---|---|---|
| OK | 200 | http://water.wa.gov.au/news/current-news | https://www.wa.gov.au/organisation/department-of-water-and-environmental-regulation *(redirected)* |
| OK | 200 | http://www.water.wa.gov.au/search-publications | https://www.wa.gov.au/search-results.html?q=&facets[0]=Publication *(redirected)* |
| OK | 200 | https://www.watercorporation.com.au/ | https://www.watercorporation.com.au/ |
| OK | 200 | https://www.der.wa.gov.au/about-us/media-statements | https://www.wa.gov.au/organisation/department-of-water-and-environmental-regulation/media-centre *(redirected)* |

### 9. General Water Quality and Sustainability / International

| Tier | Status | URL | Final URL (after redirects) |
|---|---|---|---|
| BLOCKED | 403 | http://www.unwater.org/ | https://www.unwater.org/ *(redirected)* |

## URLs needing manual review (13 total)

These returned 404 or network errors — the path has likely changed.
Check each in a browser and find the correct news / media-release URL.

| Tier | URL |
|---|---|
| ERR:ConnectError | https://www.igmdb.gov.au/reviews |
| 404 | https://www.mdba.gov.au/publications/all-publications |
| ERR:ConnectError | https://minister.awe.gov.au/ |
| ERR:ConnectError | https://minister.awe.gov.au/littleproud |
| ERR:ConnectError | http://gabcc.org.au/12094/GAB-News/ |
| ERR:ConnectError | https://www.mdbrc.sa.gov.au/ |
| ERR:ConnectError | https://www2.delwp.vic.gov.au/media-centre/news-and-announcements |
| 404 | http://www.ccma.vic.gov.au/Home.aspx |
| ERR:ConnectError | http://www.malleecma.vic.gov.au/ |
| 404 | http://www.sydneywater.com.au/SW/about-us/our-publications/Media/index.htm |
| 404 | https://www.qldwater.com.au/e-flashes-2020 |
| 404 | https://dpipwe.tas.gov.au/hot-topics |
| 404 | http://www.taswater.com.au/News/Media-Releases |

