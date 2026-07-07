# Deferred referral-company endpoint probe log

Goal: find a public JSON jobs endpoint for the 5 deferred referral companies
(Microsoft, Ford, Meta, Tesla, Dell) so they can get a fetcher. Rule: >= 2 min
between EVERY live request; take our time. Findings appended below by the probe script.

## Ford (Oracle CE) - 2026-07-07T11:49:59
- GET https://efds.fa.em5.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions
- params={'onlyData': 'true', 'expand': 'requisitionList.secondaryLocations,flexFieldsFacet.values', 'finder': 'findReqs;siteNumber=CX_1,limit=20,sortBy=POSTING_DATES_DESC'}
- status=200  content-type=application/json  len=54187
- job-like arrays (path | count | first-keys):
    .items  | 1 | ['SearchId', 'Keyword', 'CorrectedKeyword', 'UseExactKeywordFlag', 'SuggestedKeyword', 'ExecuteSpellCheckFlag', 'Location', 'LocationId', 'Radius', 'RadiusUnit', 'SelectedTitlesFacet', 'SelectedCategoriesFacet', 'SelectedPostingDatesFacet', 'SelectedLocationsFacet']
    .links  | 3 | ['rel', 'href', 'name', 'kind']
- FIRST RECORD at .links:
  {"rel": "self", "href": "https://efds.fa.em5.oraclecloud.com:443/hcmRestApi/resources/11.13.18.05/recruitingCEJobRequisitions", "name": "recruitingCEJobRequisitions", "kind": "collection"}

## Microsoft (gcsservices) - 2026-07-07T11:52:02
- GET https://gcsservices.careers.microsoft.com/search/api/v1/search
- params={'q': 'engineer', 'lc': 'United States', 'l': 'en_us', 'pg': 1, 'pgSz': 20, 'o': 'Recent', 'flt': 'true'}
- ERROR SSLError(MaxRetryError('HTTPSConnectionPool(host=\'gcsservices.careers.microsoft.com\', port=443): Max retries exceeded with url: /search/api/v1/search?q=engineer&lc=United+States&l=en_us&pg=1&pgSz=20&o=Recent&flt=true (Caused by SSLError(SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, certificate is not valid for \'gcsservices.careers.microsoft.com\'. (_ssl.c:1032)")))'))

## Tesla (cua-api) - 2026-07-07T11:54:03
- GET https://www.tesla.com/cua-api/apps/careers/state
- params={'deviceType': 'desktop', 'country': 'US', 'query': ''}
- status=403  content-type=text/html  len=414
- NOT JSON. body[:300]='<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY>\n<H1>Access Denied</H1>\n \nYou don\'t have permission to access "http&#58;&#47;&#47;www&#46;tesla&#46;com&#47;cua&#45;api&#47;apps&#47;careers&#47;state&#63;" on this server.<P>\nReference&#32;&#35;18&#46;4704d3cb&#46;1783396443&#46;a86ea231\n<P>htt'

## Meta (metacareers page) - 2026-07-07T11:56:03
- GET https://www.metacareers.com/jobs
- params=None
- status=400  content-type=text/html; charset="utf-8"  len=1543
- NOT JSON. body[:300]='<?xml version="1.0"?>\n<!DOCTYPE html>\n<html lang="en" id="facebook">\n  <head>\n    <title>Error</title>\n    <meta charset="utf-8"/>\n    <meta http-equiv="Cache-Control" content="no-cache"/>\n    <meta name="robots" content="noindex,nofollow"/>\n    <style>\n      html, body { color: #333; font-family: \''

## Dell (workday) - 2026-07-07T11:58:04
- POST https://dell.wd1.myworkdayjobs.com/wday/cxs/dell/External/jobs
- params=None
- status=200  content-type=application/json  len=66
- job-like arrays (path | count | first-keys):

=== probe run complete ===

## Ford round2 (field dump) - 2026-07-07T11:59:35
- status=200  items[0] keys=['SearchId', 'Keyword', 'CorrectedKeyword', 'UseExactKeywordFlag', 'SuggestedKeyword', 'ExecuteSpellCheckFlag', 'Location', 'LocationId', 'Radius', 'RadiusUnit', 'SelectedTitlesFacet', 'SelectedCategoriesFacet', 'SelectedPostingDatesFacet', 'SelectedLocationsFacet', 'LastSelectedFacet', 'Facets', 'Offset', 'Limit', 'SortBy', 'TotalJobsCount', 'Latitude', 'Longitude', 'SiteNumber', 'JobFamilyId', 'PostingStartDate', 'PostingEndDate', 'SelectedWorkLocationsFacet', 'RequisitionId', 'CandidateNumber', 'WorkLocationZipCode', 'WorkLocationCountryCode', 'SelectedFlexFieldsFacets', 'OrganizationId', 'SelectedOrganizationsFacet', 'UserTargetFacetName', 'UserTargetFacetInputTerm', 'HotJobFlag', 'WorkplaceType', 'SelectedWorkplaceTypesFacet', 'BotQRShortCode', 'requisitionList', 'categoriesFacet', 'locationsFacet', 'postingDatesFacet', 'titlesFacet', 'workLocationsFacet', 'flexFieldsFacet', 'organizationsFacet', 'workplaceTypesFacet']
- TotalJobsCount=827
- requisitionList count=5
- requisitionList[0] keys: ['Id', 'Title', 'PostedDate', 'PostingEndDate', 'Language', 'PrimaryLocationCountry', 'GeographyId', 'HotJobFlag', 'WorkplaceTypeCode', 'JobFamily', 'JobFunction', 'WorkerType', 'ContractType', 'ManagerLevel', 'JobSchedule', 'JobShift', 'JobType', 'StudyLevel', 'DomesticTravelRequired', 'InternationalTravelRequired', 'WorkDurationYears', 'WorkDurationMonths', 'WorkHours', 'WorkDays', 'LegalEmployer', 'BusinessUnit', 'Department', 'Organization', 'BusinessUnitId', 'LegalEmployerId', 'OrganizationId', 'MediaThumbURL', 'ShortDescriptionStr', 'PrimaryLocation', 'Distance', 'TrendingFlag', 'BeFirstToApplyFlag', 'Relevancy', 'WorkplaceType', 'ExternalQualificationsStr', 'ExternalResponsibilitiesStr', 'secondaryLocations']
- requisitionList[0]: {"Id": "62033", "Title": "Engineering Program Manager", "PostedDate": "2026-07-06", "PostingEndDate": null, "Language": "US", "PrimaryLocationCountry": "US", "GeographyId": 300000009182319, "HotJobFlag": false, "WorkplaceTypeCode": "ORA_HYBRID", "JobFamily": null, "JobFunction": null, "WorkerType": null, "ContractType": null, "ManagerLevel": null, "JobSchedule": null, "JobShift": null, "JobType": null, "StudyLevel": null, "DomesticTravelRequired": null, "InternationalTravelRequired": null, "WorkDurationYears": null, "WorkDurationMonths": null, "WorkHours": null, "WorkDays": null, "LegalEmployer": null, "BusinessUnit": null, "Department": null, "Organization": null, "BusinessUnitId": 300000004335154, "LegalEmployerId": 300000004907524, "OrganizationId": 300000004335154, "MediaThumbURL": null, "ShortDescriptionStr": "We are building a world-class software and cloud service engineering team and are excited to grow our Engineering Program Management (EPM) practice.\n\nIn this role, you will be an EPM within a Vehicle Cloud & Mobile (VC&M) portfolio, responsible for owning the system of delivery—ensuring domain work is focused on the most important outcomes, aligned to strategy, and planned and governed in a consistent, transparent way.", "PrimaryLocation": "Dearborn, MI, United States", "Distance": 1783296000000.0, "TrendingFlag": true, "BeFirstToApplyFlag": false, "Relevancy": 9, "WorkplaceType": "Hybrid", "ExternalQualificationsStr": null, "ExternalResponsibilitiesStr": null, "secondaryLocations": []}

## Microsoft round2 (verify=False diagnostic) - 2026-07-07T12:01:38
- status=404  content-type=text/html  len=266389
- NOT JSON: '<!DOCTYPE html>\n<html lang="en" dir="ltr">\n  <head>\n    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />\n    <title>Page not found</title>\n\n    <meta http-equiv="X-UA-Compatible" content="IE=edge" />\n    <meta name="msapplication-config" content="none" />\n    <link rel="icon" ty'

=== round 2 complete ===
