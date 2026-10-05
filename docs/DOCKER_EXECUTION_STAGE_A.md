# Docker: результаты этапа A

Дата: 2026-09-28. **Этап A не завершён; production Docker execution не реализован.**
План B–E не выполнен. Включить защищённый режим пока нельзя.

## Что реализовано и проверено

- Восстановлена привязка Orca к живому терминалу координатора. Причиной ошибок
  `terminal_handle_stale` / `no_active_sender_terminal` был устаревший handle.
  Перезапуск Orca не потребовался. Делегирование выполнялось через Orca/OpenCode,
  без `--model` и изменений конфигурации модели, с отдельными worktrees для записи.
- Добавлены диагностические контроллеры в `scripts/`: cgroup/отмена,
  macOS disk-image quota, сетевая граница, fixed-origin AI gateway и реальный
  OpenCode request. Это прототипы, а не executor приложения.
- Исправлен опасный путь в `Config.load`: секция `execution` больше не игнорируется
  как произвольная front-конфигурация. Любая её форма, а также непустые
  `MERGERAIL_EXECUTION_BACKEND` / `MERGERAIL_EXECUTION_REQUIRED`, отклоняются до
  определения Git-ветки, checks и backend probes. Сообщение прямо запрещает host fallback.
  Прежние конфигурации без запроса Docker остаются локальными и не становятся защищёнными.
- Добавлены тесты раннего отказа и шлюза, включая DNS pinning, фиксированные
  Host/SNI, запрещённые маршруты/заголовки, лимиты потока и абсолютный deadline
  против медленной отправки HTTP headers.
- Полный Python suite: **467 passed**, выполнен внутри контейнера без сети и bind mounts.
  Источник и pure-Python pytest dependencies передаются через stdin; запись — в tmpfs
  128 MiB; CPU 1, RAM 512 MiB, swap 0, PIDs 128, read-only rootfs, UID 65534.
  Coverage, mypy и webui build этим прогоном не измеряются.
- Ruff 0.16.1: все девять изменённых/добавленных Python-файлов прошли проверки
  E, F, I, UP, B, SIM, RUF без замечаний (Python 3.11, line-length 100).
  Проверка выполнена официальным pinned image через stdin: без mounts и сети,
  0,5 CPU / 128 MiB / PIDs 32, read-only rootfs, UID 65534.

## Среда и границы доказательств

Проверен только **macOS arm64 → Docker Desktop 4.79.0 / Engine 29.5.3 /
Linux arm64 / cgroup v2**, без эмуляции. Хост: 24 GiB RAM, 12 логических CPU.
Docker engine сообщает бюджет RAM 4 108 075 008 байт (3,83 GiB).
Это не пиковый расход всей VM. Один замер RSS процесса Apple Virtualization —
около 1,31 GiB; он включает чужие нагрузки и не является изолированным benchmark.

Linux-хост, Windows/WSL, amd64 и отдельное слабое устройство не проверены.
Общие resource/network probes подготовлены для повторения с native image;
macOS storage probe намеренно отклоняет другие платформы.

Глобальные настройки Docker/WSL, чужие контейнеры и Docker context не менялись.
Нет push, stash, reset или clean пользовательского checkout. Исходный незакоммиченный
план сохранён, в него добавлены только фактические статусы. Production bundle sync,
immutable delivery и восстановление пользовательского результата пока отсутствуют.

## Реальные измерения

| Проверка | Факт |
| --- | --- |
| CPU | 0,5 CPU: два busy loops за 3 s получили около 1,5 CPU-s, есть throttling |
| RAM | Попытка выделить 128 MiB при лимите 64 MiB: OOMKilled=true, exit 137 |
| PIDs | При лимите 32 fork после 31 дочернего процесса получает EAGAIN |
| tmpfs | Запись 9 MiB в 8 MiB останавливается на 8 388 608 B, ENOSPC |
| Отмена | Контейнер с игнорирующим TERM процессом и ребёнком остановлен за 1,095 s |
| Постоянная квота | Фиксированный образ 67 108 864 B; ENOSPC на 64 880 640 B |
| Disk-probe RAM | memory.peak 60 264 448 B; контейнер не OOM |
| Сеть | 12/12 checks; итоговый прогон 2,716 s, очистка подтверждена |
| OpenCode | Настроенная по умолчанию модель Space Bunny Free; реальный keyless ответ через шлюз |
| AI runtime RAM | memory.peak 352 391 168 B и 345 792 512 B в двух прогонах |
| AI wall time | Request 3,832 / 7,821 s; весь прототип 8,103 / 13,597 s |
| Python tests | 467 passed за 19,96 s; весь запуск 20,695 s; memory.peak 98 308 096 B |

AI-контейнер ограничен 1 CPU / 2 GiB / PIDs 128, шлюз — 0,1 CPU / 128 MiB / PIDs 32.
Это не полный eco benchmark: ещё нет последовательности fixer → immutable checks →
reviewer, пользовательского lease, cache reuse и idle-stop executor.
Cold pull, VM peak RAM, native baseline и цель warm overhead ≤5 s не подтверждены.

Машиночитаемые результаты:
[ресурсные stress probes](DOCKER_STAGE_A_STRESS_RESULTS.json),
[сеть](DOCKER_NETWORK_STAGE_A_RESULTS.json),
[диск](DOCKER_MACOS_STORAGE_PROBE_RESULTS.json),
[AI](DOCKER_AI_STAGE_A_RESULTS.json),
[повторный AI](DOCKER_AI_STAGE_A_WARM_RESULTS.json),
[полный Python suite](DOCKER_STAGE_A_TEST_RESULTS.json).
`release_ready=false` в прототипах остаётся обязательным.

## Сеть и авторизация

Недоверенный клиент подключён только к `--internal` bridge с
`gateway_mode_ipv4=isolated`, IPv6 отключён. Нет default route.
Внешний DNS отключён через `--dns=127.0.0.1`; один HTTP_PROXY не считается защитой.

Проверены разрешённый fixture, отказ прямого доступа к 1.1.1.1:443,
169.254.169.254:80 и созданному контроллером host sentinel. Адрес
`host.docker.internal` сначала проверяется положительным контролем из отдельного
ограниченного доверенного контейнера; отрицательный тест обязан использовать тот же IP.
Sentinel кратковременно слушает произвольный свободный порт, не принимает команд и
закрывается после прогона. Случайное чужое подключение даёт безопасный ложный отказ.

Шлюз разрешает только фиксированные API routes одного выбранного контроллером origin.
Он один раз разрешает DNS, отвергает private/link-local/metadata и переходные адреса,
подключается к pinned IP, проверяет TLS для фиксированного hostname и сам задаёт Host.
CONNECT, redirects, произвольные targets, Transfer-Encoding и Upgrade отклоняются.
Request ≤8 MiB, response ≤20 MiB; максимум два запроса, deadlines, без логирования
credentials/тел запросов. Это лимит, а не обещание поддержки неограниченного параллелизма.

Реальный OpenCode 1.18.33 получил `MERGERAIL_STAGE_A_OK` через этот шлюз без чтения
или передачи auth.json. Используется запись текущей default-модели из локального
каталога, без CLI `--model` и изменения host config. В контейнер передаётся очищенная
конфигурация без host plugins/MCP, auto-update и project config.
**Доказан только keyless OpenCode Zen free path.** API-key, OAuth, Claude/Codex,
external backends, session resume и dependency egress ещё не валидированы.

## Блокер: lifecycle постоянного хранилища

Обычный local volume отклоняет квоту:
`quota size requested but no quota support`.
Принятый `storage-opt=size` не доказывает квоту подключённых volumes.

Альтернатива на macOS: отдельный фиксированный UDIF, case-sensitive journaled HFS+.
ENOSPC и предел размера доказаны без sudo, privileged или передачи исходного repo.
Однако после удаления контейнера процесс `com.apple.Virtualization.VirtualMachine`
удерживает descriptors образа, включая уже удалённый filler. Свободно лишь 61 440 B.
Обычный detach возвращает **`Resource busy`**; detach/reattach persistence и recovery
не проверены. Безопасного способа освободить эти handles отдельно от общей VM
в выполненном исследовании не найдено.

Сохранён один синтетический образ 64 MiB:
`~/Library/Caches/mergerail-macos-storage-probe/probe-idvbjf95/quota.dmg`.
Его контейнер удалён. Повторное создание образа заблокировано, пока предыдущий
сохранён: бесконечного накопления probes нет. Образ не содержит пользовательского
репозитория или credentials. Удалять каталог при активном mount нельзя.
Force-detach, перезапуск общей VM и остановка чужих контейнеров не выполнялись.

Перезапуск Docker когда-либо может позволить очистить этот тестовый образ, но сам
по себе не доказывает пригодность схемы для production. Следующий кандидат —
отдельное локальное Linux-окружение с quota-capable filesystem (например XFS с
project quotas), cgroup v2 и независимым бюджетом. Сначала нужны реальные ENOSPC,
освобождение места, stop/restart и crash-recovery проверки. На этом Mac такого
окружения нет; Lima/Colima не установлены. Его создание требует отдельного решения
об установке и ресурсах, а не скрытой смены настроек существующего Docker.

## Повторение проверок

Скрипты не выполняют pull автоматически. Нужны указанные в них заранее проверенные
native images по digest. Во время работы загружены только официальные pinned images
после проверки manifest/размера и свободного места; пользовательские Dockerfile не строились.

```sh
python3 scripts/docker_stage_a.py --stress \
  --image python@sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f
python3 scripts/docker_network_stage_a.py
python3 scripts/docker_ai_stage_a.py --report /tmp/mergerail-ai.json
python3 scripts/docker_stage_a_unit_tests.py \
  --site .venv/lib/python3.13/site-packages \
  --report /tmp/mergerail-tests.json tests
```

Unit harness копирует только указанные pure-Python pytest packages из уже имеющейся
venv; ничего не устанавливает на хост. Путь `--site` зависит от локальной venv.
Resource/network scripts: exit 2 означает успешные частичные probes при закрытом
release gate; exit 1 — ошибка. AI/unit scripts возвращают 0 только за свой узкий успех.
Storage script сейчас повторно откажет из-за сохранённого образа.

## Независимое ревью и нарушения процедуры

Orca researcher составил карту всех путей запуска; security reviewer отдельно проверил
network/storage предложения, затем реализацию шлюза и probes. Координатор проверил код,
исправил обнаруженные ошибки и повторил Docker-проверки. Среди исправлений:
реальный host-gateway positive control, таймаут DNS как неопределённость, абсолютный
header deadline, восстановление socket timeout и запрет anonymous volumes в storage probe.
Рекомендация увеличить число запросов не принята: лимит два остаётся намеренным.
Мировая запись внутри synthetic mount ограничена родительским каталогом mode 0700.

Не все предварительные действия воркеров соответствовали инструкциям:
два автора запускали вспомогательные mock harnesses на хосте; автор config guard
запустил Docker tests с read-only host worktree/site-packages binds без обязательных
CPU/RAM/PID limits. Координатор остановил этот dispatch через Orca; его контейнер
исчез, дополнительных контейнеров от этого запуска не осталось. Эти результаты
**не являются доказательством соблюдения изоляции**. Финальные 467 tests и перечисленные
выше acceptance probes выполнены контроллером с ограничениями; инцидент не скрывается.

Завершённые supervised workers освобождены по протоколу. Один security-review terminal
Orca пометила `retained: user_takeover`; координатор его не закрывал. Worktrees сохранены.

## Что остаётся

Этап A: lifecycle/quota recovery, API-key/OAuth и остальные backends, полный
CLI → tests → reviewer, idle, platform/resource matrix.
B–D: executor, все runtime/probe paths, общий lease, Git bundles/quarantine,
неизменяемые проверенные SHA, dirty checkout/CAS delivery, recovery, caches и UI.
E: независимый review самой Git-доставки и production host/container boundary,
реальные end-to-end tests, остальные ОС/архитектуры и слабое устройство.
Guard конфигурации не заменяет эти реализации и не означает завершение задачи.

## Источники

- [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)
- [Docker isolated gateway mode](https://docs.docker.com/engine/network/port-publishing/#gateway-modes)
- [Docker Engine 28: удаление BridgeNfIptables из API 1.50](https://docs.docker.com/engine/release-notes/28/)
- [Docker volumes](https://docs.docker.com/engine/storage/volumes/)
- [OpenCode v1.18.33](https://github.com/anomalyco/opencode/tree/v1.18.33)
- Локальные `hdiutil(8)`, `unmount(2)`, `hdiutil info` и `lsof`: проверка
  занятости относится к текущему хосту, а не ко всем Docker Desktop установкам.
