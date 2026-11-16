# Проверка (выполняет Claude)

Прочитать одну строку:
```
gh api "repos/ToTmss/btc-signal/contents/summary.json?ref=data" --jq .content | base64 -d | jq -r '.brief, .generated_utc'
```
Если `generated_utc` старше 10 мин — сводка не обновляется: проверить позицию напрямую в Wallet и сообщить.

| В строке | Действие |
|---|---|
| `нет сигнала`, `без действий` | ничего не вызывать, статус одной строкой |
| `SIGNAL ...` | `balance_get` → `perps_prepare_order_place` (contractId 1, market, leverage 40, amount = баланс − 1.2, stop/tp из сигнала) → execute → `perps_get_order`; записать `position.json` (side, entry=filled_price, stop, tp, orig_stop=stop, opened_utc) |
| `ACTION do=move_stop` / `move_tp` | `perps_prepare_auto_close_set` с новым уровнем → execute; обновить stop/tp в `position.json` |
| `ACTION do=close` | `perps_prepare_position_close` → execute → дописать сделку в `trades.json`, `position.json` = `{}` |
| `ACTION do=verify_closed` | `perps_list_positions`; если позиции нет — `balance_get`, дописать в `trades.json` (pnl = баланс − баланс до входа), `position.json` = `{}` |

Запись файла на main: `gh api -X PUT repos/ToTmss/btc-signal/contents/<file> -f message=... -f content=<base64> -f sha=<sha>`.

Защита на бирже: у любой позиции всегда стоят стоп и тейк в самом ордере. Скрипт блокирует входы против общей картины, при R:R < 1,5 после комиссий, за 15 мин до/30 мин после данных США, после 2 убыточных сделок за день и 30 мин после убытка.
