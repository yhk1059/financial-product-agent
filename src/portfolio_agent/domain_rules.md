## 공통
- 모든 데이터는 스냅샷이며 대부분 2026-08-21 기준이다. 답변에 기준일을 밝힌다. 만기·경과기간 계산의 기준일도 CURRENT_DATE가 아니라 DATE '2026-08-21'을 쓴다. (상품별 기준일 컬럼: pref01n001.du_upt_dt, prfd01n001.fd_price_bas_dt, prbd01n001.info_base_dt)
- 수익률·보수율·변동성·괴리율·쿠폰·민평금리 등 비율 컬럼은 이미 % 단위 숫자다(13.2 = 13.2%). 100을 곱하거나 나누지 않는다.
- 금액 컬럼(순자산, AUM, 거래대금, 발행금액)은 원 단위다. 답변할 때 억원·조원으로 환산해서 보여준다.
- 결측률이 높은 컬럼으로 정렬·필터할 때는 `IS NOT NULL` 조건을 넣고, 상위 N개 정렬에는 `NULLS LAST`를 쓴다. 답변에 "전체 N건 중 값이 있는 M건 기준"을 밝힌다.
- 사용자가 따로 말하지 않으면 현재 판매·거래 가능한 상품만 대상으로 한다.
- 상품명 검색은 `ILIKE '%키워드%'`를 쓴다.

## pref01n001 (국내 ETF·ETN)
- ETF/ETN 구분: pd_grp_no = 'ETF' | 'ETN'
- 판매중: pd_sale_yn = '1'. 거래정지가 아닌 것: pd_tr_yn = '0'
- 자산군: wu_inv_ast_type(주식/채권/원자재/대체투자/혼합자산/단기자금/통화/부동산/기타). 투자지역: wu_inv_rgn(국내/미국/글로벌/중국 …)
- 총보수 cu_charge_rt는 text이고 ETF의 대부분이 비어 있다. 값이 '0'인 행도 실제 보수가 아니라 미입력이다. 비교할 때는 `cu_charge_rt::numeric > 0` 조건을 넣고, 결측이 많다는 점을 반드시 알린다.
- 레버리지: `cu_lev_fector::numeric > 1`. 인버스: `cu_lev_fector::numeric < 0`. (text이므로 numeric 변환 필요)
- 운용사: cu_fund_mgmt_co에는 운용사명이 아니라 신탁명이 섞여 있다. 운용사를 물으면 ref_fund_mgmt_co(영문, 결측 32%)를 쓰거나 상품명 브랜드로 판단한다. 브랜드는 KODEX=삼성, TIGER=미래에셋, RISE=KB, ACE=한국투자, SOL=신한, PLUS=한화, KIWOOM=키움, HANARO=NH-Amundi 등이다.
- 분배: pd_dvid_yield(연환산 분배수익률 %), pd_dvid_cycl(A=연, S=반기, Q=분기, M=월)
- 위험등급: pd_risk_nm('매우높은위험(1등급)' ~ '매우낮은위험(6등급)'). 1등급이 가장 위험하다.
- 연금계좌 가능: pd_pen_tr_yn = 'Y'

## pref02n001 (해외 ETF·ETN)
- 모두 미국 상장이고 USD 기준이다. 상품번호 pd_itm_no는 RIC, 티커는 pd_abrv_nm에 있다.
- 자산군 wu_inv_ast_type은 영문이다(Equity/Alternatives/Bond/Mixed Assets/Commodity/Money Market).
- 보수: cu_charge_rt(numeric, %). 0인 행(419건, 예: ARKK)은 실제 보수가 아니라 미입력이므로 보수 비교·정렬 시 `cu_charge_rt > 0` 조건을 넣는다.
- 레버리지: `cu_lev_fector > 1`(NULL이면 일반 1배 상품). 인버스: `cu_lev_fector < 0` 또는 cu_inverse_short_yn = 'Y'. 레버리지를 물으면 인버스는 제외한다.
- du_last_nav(88% 결측)와 du_diff_rt(사실상 전부 결측)는 쓰지 않는다. 수익률은 1일 수익률(du_er_1d)만 있다.

## prfd01n001 (펀드)
- 판매중: sale_yn = '판매중'. 공모/사모: prvo_pbff_desc = '공모' | '사모'
- 사모펀드는 순자산·수익률·제로인 유형·위험등급이 거의 없다. 성과·유형 질문은 기본으로 `prvo_pbff_desc='공모' AND sale_yn='판매중'`을 대상으로 한다.
- 유형: zrin_btyp_nm(제로인 대유형: 주식형/채권형/해외주식형 …) 또는 or_attr_desc(주식형/채권형/재간접/MMF …)
- 수익률: fd_mm1/3/6_ern_r, fd_yr1/2/3/5_ern_r (%). fd_wk1_ern_r는 전부 비어 있다.
- 총보수는 or_co_rwrd_r + sale_co_rwrd_r + trusc_rwrd_r + ofwk_trus_rwrd_r(각각 %)로 계산한다.
- 한 펀드에 여러 클래스(A, C, C-e 등)가 있어 itm_nm이 비슷한 행이 여러 개다. han_clas_nm은 클래스명이다.
- 운용사명 컬럼은 없다. 운용사 코드는 or_co_xtn_itt_cd다(mtco_itm_no는 운용사 내부 종목번호이지 운용사가 아니다). 운용사별 집계는 `GROUP BY or_co_xtn_itt_cd`로 하고 `MIN(itm_nm) AS sample_fund_name`을 함께 조회해, 펀드명 앞부분(예: '미래에셋', '삼성', 'KB')으로 운용사명을 추정해 보여준다.
- 위험등급 zrin_fd_ivst_risk_grd_nm에는 '높은 위험'과 '높은위험'처럼 띄어쓰기만 다른 값이 섞여 있다. `replace(col,' ','')`로 비교한다.

## prbd01n001 (채권)
- 같은 채권(pd_no)이 장내·장외 행으로 중복될 수 있다. 채권 개수를 셀 때는 `COUNT(DISTINCT pd_no)`를 쓴다.
- 장내/장외: pd_exg_mkt = '장내' | '장외'. 공모/사모: bd_ofr_tcd
- 당사 판매(매수 가능) 채권: trade_price IS NOT NULL (634행). 매수수익률 buy_yield, 세후수익률 after_tax_yield, 매수가능수량 buyable_quantity도 이 행에만 있다.
- 시가평가 금리: applied_yield(민평수익률 %). 장내 종가수익률: exg_close_yield
- 만기: mat_dt(date), 잔존일수 remaining_days. "만기 N년 이내"는 `mat_dt <= DATE '2026-08-21' + INTERVAL 'N years'` 또는 `remaining_days <= N*365`로 처리한다.
- 신용등급 crd_grd(18% 결측) 순서는 AAA > AA+ > AA0 > AA- > A+ > A0 > A- > BBB+ > BBB0 > BBB- > BB0 > BB- > B+ > B- > C0 이다. "AA- 이상"은 `crd_grd IN ('AAA','AA+','AA0','AA-')`처럼 목록으로 비교한다(문자열 대소 비교 금지).
- 분류: std_pd_mcls_nm(회사채/특수채/국공채), std_pd_scls_nm(일반사채/국고채/은행채 …)
- 발행기관: pd_pbcm

## domestic_etf_holdings (국내 ETF 구성종목)
- item_id → pref01n001.pd_itm_no 로 조인한다.
- 편입 비중·수량 컬럼은 없다. "많이 편입"은 편입한 ETF 수로만 답하고, 비중은 알 수 없다고 밝힌다.
- 종목 검색은 component_name으로 한다(예: '삼성전자'). component_code는 15% 결측이며 형식도 섞여 있다.
- 같은 ETF 안에 같은 종목명이 중복될 수 있다. ETF 수를 셀 때는 `COUNT(DISTINCT item_id)`를 쓴다.
- 현금성 항목은 종목 집계에서 제외한다: `component_name NOT IN ('원화예금','설정현금액','원화현금','현금성자산','100%현금설정액')`
