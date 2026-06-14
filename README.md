# 사무실 월드컵 토토 - 서버

## 실행
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## 관리자 토큰
`main.py` 상단의 `ADMIN_TOKEN`을 변경하세요.
API 호출 시 헤더에 `X-Admin-Token: <토큰>` 추가.

## 주요 API
| Method | Path | 설명 |
|--------|------|------|
| GET | /api/config | 베팅금액·카카오페이 링크 조회 |
| POST | /api/bets | 베팅 제출 |
| GET | /api/bets | 베팅 목록 조회 |
| GET | /api/results | 경기 결과 조회 |
| PATCH | /api/admin/config | 베팅금액·링크 수정 (관리자) |
| GET | /api/admin/bets | 전체 베팅 조회 (관리자) |
| PATCH | /api/admin/bets/{id}/payment | 입금확인 토글 (관리자) |
| PATCH | /api/admin/bets/{id}/payout | 배당완료 토글 (관리자) |
| PUT | /api/admin/results/{game_id} | 경기 결과 등록 (관리자) |
| DELETE | /api/admin/results/{game_id} | 경기 결과 삭제 (관리자) |
| DELETE | /api/admin/bets/{id} | 베팅 삭제 (관리자) |

## 데이터
`data/` 폴더에 JSON 파일로 저장됩니다.
- `bets.json` : 베팅 내역
- `config.json` : 설정 (베팅금액, 카카오페이 링크)
- `results.json` : 경기 결과
