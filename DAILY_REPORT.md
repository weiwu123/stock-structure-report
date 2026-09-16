# Daily Structure Report

排程明確指定 `Asia/Taipei`：`5 8 * * 2-6`，即台灣週二至週六早上 08:05，
對應美股週一至週五收盤後。另於 08:35 執行補跑檢查。
NYSE 交易日曆處理假日、夏令時間與提早收盤；沒有新的交易日且資料已完整時跳過。
手動 Run workflow 會強制重新產生。

GitHub Actions 的 schedule 可能延遲或漏發，兩個排程都沒有準點保證。
若需要嚴格的 08:05 觸發，需使用獨立常駐主機或外部排程服務；本修改未部署外部服務。
在 Actions 的 Summary 可查看觸發方式、實際開始時間、資料交易日及發布 commit。

## 資料與發布

- `structure_analysis_charts.py` 輸出 `structure_data.json` 和 `report_charts.html`。
- 保留既有 JSON 欄位，新增 schema_version、as_of_date、quality，及每檔 history_rows/data_status。
- 只接受最新已完成交易日的日 K；下載或分析失敗最多嘗試三次。
- 成功率至少 90%，且上一版已發布、仍在追蹤名單的股票不得消失。
  不符合時執行失敗並保留舊檔，不把過期行情混入新報告。
- 新追蹤股票若無資料，在未低於門檻時可發布部分結果，但會在 HTML、JSON 與 Actions 標示缺漏。
  部分結果不視為完成，08:35 仍會重試。
- 原版報告與盤前快照失敗，不阻止有效結構報告發布；各自保留舊輸出。
- 排程與手動執行使用相同 concurrency group；發布 push 可重試，遇到 rebase 衝突則明確失敗。

## 驗證與上線

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python report_pipeline.py
```

將程式、workflow、requirements.txt、report_pipeline.py 和 tests 一起提交至 GitHub 的 main，
再至 Actions → Daily Structure Report → Run workflow，確認報告產生與提交成功。
排程必須位於預設分支且 workflow 為啟用狀態；公開 repository 長期無活動可能自動停用。

目前儀表板會每 30 分鐘同步 JSON；這次未變更儀表板程式，也未加入儀表板端的過期訊號限制。
來源報告失敗時，儀表板仍可能顯示上一份資料，應查看 Structure 日期與 Actions 執行狀態。
