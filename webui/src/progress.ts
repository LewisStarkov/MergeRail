import type {Task} from "./types";

export const ROLES = {
  fixer: {name: "Разработчик · Fixer", purpose: "Меняет код и исправляет замечания ревьюера."},
  reviewer: {name: "Ревьюер · Reviewer", purpose: "Независимо проверяет изменения и выносит вердикт. Код не меняет."},
  runner: {name: "MergeRail · система", purpose: "Сохраняет результат, запускает проверки и применяет одобренные изменения."},
  devbot: {name: "DevBot · публикация", purpose: "Разворачивает одобренный результат и сообщает итог публикации."},
};

export function agentRole(name: string) {
  return ROLES[name.toLowerCase() as keyof typeof ROLES];
}

type StepState = "pending" | "current" | "complete" | "attention" | "optional" | "unknown";
export function taskProgress(task: Task) {
  const deployment = task.deployment?.status;
  const integrated = task.delivery?.status === "succeeded";
  const applying = task.status === "delivering" || task.status === "approved";
  const terminal = ["failed", "blocked", "cancelled", "closed", "cancelling", "done", "new"].includes(task.status);
  const fixerStatus = task.live?.roles?.fixer?.status;
  const fixing = !terminal && !applying && Boolean(fixerStatus && !["complete", "failed"].includes(fixerStatus));
  const checking = !terminal && !applying && !fixing && task.live?.stage === "running checks";
  const reviewing = !terminal && !applying && !fixing && !checking && (task.status === "review" || task.live?.stage === "reviewing");
  let index = applying ? 3 : reviewing ? 2 : checking ? 1 : 0;
  let title = "Разработчик вносит изменения";
  let actor = ROLES.fixer.name;
  let detail = "Выполняет ваш запрос в рабочей ветке. После изменений результат проверит MergeRail.";
  let next = "Общие проверки → независимое ревью.";
  let attention = false;
  if (checking) {
    title = "MergeRail запускает проверки"; actor = ROLES.runner.name;
    detail = "Проверяет результат командами проекта. Отчёт разработчика о тестах не заменяет этот этап.";
    next = "При успехе — ревью; при ошибках — исправления разработчика.";
  } else if (reviewing) {
    title = "Ревьюер проверяет результат"; actor = ROLES.reviewer.name;
    detail = "Общие проверки пройдены. Отдельный агент оценивает изменения и решает, можно ли их принять.";
    next = "Одобрение → применение; замечания → исправления и повторные проверки.";
  } else if (applying) {
    title = "MergeRail применяет одобренный результат"; actor = ROLES.runner.name;
    detail = "Доставляет изменения выбранным способом и проверяет интеграцию.";
    next = "Завершение доставки. Публикация — если настроена.";
  }
  if (!terminal && task.execution?.phase === "resuming-checkpoint") {
    title = "Восстанавливается сессия из сохранённого результата"; actor = ROLES.runner.name;
    detail = "Завершение агента ещё не подтверждено. MergeRail продолжает ту же сессию из checkpoint.";
    next = "Подтверждение завершения → общие проверки → независимое ревью.";
  }
  if (task.status === "new") {
    title = "Задача в очереди"; actor = "Агент ещё не запущен";
    detail = "Запрос сохранён и ожидает выполнения."; next = "Разработчик начнёт работу.";
  } else if (["failed", "blocked"].includes(task.status)) {
    title = task.status === "failed" ? "Выполнение остановилось с ошибкой" : "Задача заблокирована";
    actor = ROLES.runner.name; attention = true;
    detail = task.execution?.result_sha
      ? "Результат сохранён. Сохранённый commit сам по себе не подтверждает завершение, проверки или одобрение."
      : "Причина остановки указана в результате задачи и журнале ниже.";
    next = "Проверьте причину остановки и выберите доступное действие внизу.";
  } else if (["cancelled", "closed", "cancelling"].includes(task.status)) {
    title = task.status === "cancelling" ? "MergeRail останавливает выполнение" : "Задача остановлена";
    actor = ROLES.runner.name; detail = "Работа над запросом прекращена.";
    next = task.status === "cancelling" ? "Дождаться остановки агента." : "Повторный запуск — через доступные действия.";
  } else if (task.status === "done") {
    actor = ROLES.runner.name; title = "Работа завершена";
    detail = integrated ? "Одобренный результат доставлен. Подробности — в блоке применения ниже." : "Результат задачи доступен ниже. Статус завершения не означает публикацию.";
    next = "Посмотреть результат.";
    if (deployment) {
      index = 4; actor = ROLES.devbot.name;
      if (deployment === "succeeded") {title = "DevBot подтвердил публикацию"; detail = "Одобренный результат опубликован."; next = "Посмотреть результат публикации ниже.";}
      else if (["failed", "blocked"].includes(deployment)) {title = "Публикация требует внимания"; detail = "Работа над кодом завершена, но DevBot не подтвердил публикацию."; next = "Проверьте ошибку публикации ниже."; attention = true;}
      else if (deployment === "superseded") {title = "Публикация заменена более новой"; detail = "DevBot пометил этот запрос как superseded."; next = "Проверьте актуальный запрос публикации.";}
      else {title = deployment === "running" ? "DevBot публикует результат" : "Результат ожидает публикации"; detail = "Запрос передан DevBot. Успешная публикация ещё не подтверждена."; next = "Дождаться результата DevBot.";}
    }
  }
  const steps = ["Изменения", "Проверки", "Ревью", "Применение", "Деплой"].map((label, step): {label: string; state: StepState} => {
    let state: StepState = terminal && task.status !== "new" ? "unknown" : "pending";
    if (!terminal && step < index) state = "complete";
    if (!terminal && step === index) state = "current";
    if (task.status === "done" && integrated && step < 4) state = "complete";
    if (step === 4) state = !deployment ? "optional" : deployment === "succeeded" ? "complete" : ["failed", "blocked"].includes(deployment) ? "attention" : deployment === "superseded" ? "optional" : "current";
    return {label, state};
  });
  return {title, actor, detail, next, attention, steps};
}
