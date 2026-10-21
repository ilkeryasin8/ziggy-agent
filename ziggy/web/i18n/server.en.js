"use strict";
// English for the Turkish texts of Python, as the panel shows them (see i18n.js). Strict JSON inside i18nAdd: tests/test_i18n.py parses it.
i18nAdd({
  "Ajanın soruları var": "The agent has questions",
  "Plan onayını bekliyor": "Plan waiting for approval",
  "Bitti, PR açılmadı": "Done, no PR yet",
  "Koşu başarısız oldu": "Run failed",
  "Komut hata verdi": "Command failed",
  "Ticket state'i okunamıyor": "Ticket state unreadable",
  "Dal PR'dan sonra değişti": "Branch changed after the PR",
  "PR kapandı ama worktree'de iş kaldı": "PR closed but the worktree still has work",
  "Oturum bir süredir sessiz": "Session quiet for a while",
  "Komut uzun sürüyor": "A command is taking long",
  "Koşunun süreci ölmüş": "The run's process died",
  "Yeni hafıza önerisi": "New memory proposal",
  "Beklediği ticket takıldı": "A ticket it waits for is stuck",
  "Yeniden düzenleme gerekiyor": "Needs a restack",
  "Başlatılmayı bekliyor": "Waiting to be started",
  "Kota doldu": "Usage limit reached",
  "Yeni iş önerisi": "New work proposal",
  "Korumalı dosya değişikliği": "Protected file change",
  "PR birleştirildi ama worktree'de hâlâ iş var": "PR merged but the worktree still has work",
  "PR kapandı ama worktree'de hâlâ iş var": "PR closed but the worktree still has work",
  "Yeniden düzenlenen dal push bekliyor": "Restacked branch waiting for a push",
  "PR'da CI kırmızı": "CI is red on the PR",
  "süreç sonuç yazmadan bitti": "the process ended without writing a result",
  "Ziggy komutu durdurmaz: takıldıysa ajana yön ver ya da durdur.": "Ziggy does not stop the command: if it is stuck, steer the agent or stop it.",
  "Hafıza önerileri": "Memory proposals",
  "Hafıza": "Memory",
  "İş önerileri": "Work proposals",
  "(state okunamıyor)": "(state unreadable)",

  "normal": "normal",
  "baskı altında": "under pressure",
  "kritik": "critical",
  "bilinmiyor": "unknown",
  "bellek baskısı kritikti": "memory pressure was critical",
  "bellek baskı altındaydı": "memory was under pressure",
  "makinenin belleği rahattı": "the machine had memory to spare",

  "zaman aşımı": "timeout",
  "sessiz kaldı": "went quiet",
  "süreç sonuçsuz bitti": "the process ended without a result",
  "hiçbir komut uzun sürmedi": "no command took long",

  "5 sa": "5h",
  "7 gün": "7d",
  "7 gün Opus": "7d Opus",
  "7 gün Sonnet": "7d Sonnet",
  "varsayılan giriş": "default login",

  "Ana dal kırmızı: başka işlerden önce yeşile dönmeli.": "The main branch is red: it should turn green before other work.",
  "Yol haritasının sıradaki maddesi.": "The next item on the roadmap.",
  "GitHub'da seni bekleyen açık issue.": "An open issue waiting for you on GitHub.",
  "Review'da ertelenmiş bir bulgu; hâlâ ticket'ı yok.": "A finding deferred in review; it has no ticket yet.",
  "yeni sinyal yok": "no new signals",
  "kota doldu": "usage limit reached",
  "gh_user yok: CI ve issue'lar okunmadı": "no gh_user: CI and issues were not read",

  "Tüm değişiklikler": "All changes",
  "CI ya da ajan dosyası": "CI or agent file",
  "bağımlılık": "dependency",
  "üretilmiş": "generated",
  "test": "test",

  "Yol haritası taslağı": "Roadmap draft",
  "bu projenin yol haritası BMad'in epics.md'sinden okunuyor; değişiklik orada yapılır": "this project's roadmap is read from BMad's epics.md; make the change there",

  "klonlanıyor": "cloning",
  "ajan inceliyor": "agent reviewing",
  "ajan onarıyor": "agent repairing",
  "deneniyor": "trying",
  "denendi": "tried",
  "denenmedi": "not tried",
  "geçmedi: hata": "failed: error",

  "Haftalık rapor": "Weekly report",
  "- yok": "- none",
  "- Teslim: bu hafta birleşen PR yok": "- Delivery: no PR merged this week",
  "## Birleşenler": "## Merged",
  "## Bitenler": "## Done",
  "## Başarısızlar": "## Failed",
  "## Hafıza": "## Memory"
});

// Python's texts with variable parts, as the panel shows them: [regex, English with $1…].
i18nPatterns([
  // attention.py: the inbox items' titles and details
  [/^(.+), (\S+) bitti; (\S+) başlasın mı\?$/, "$1, $2 done; start $3?"],
  [/^(\S+) bitti; (\S+) başlasın mı\?$/, "$1 done; start $2?"],
  [/^(\S+) başlasın mı\?$/, "Start $1?"],
  [/^Kontrollü modda bağımlı ticket'lar kendiliğinden başlamaz: (\S+) senin onayınla başlar\.$/,
    "In controlled mode, dependent tickets do not start by themselves: $1 starts with your approval."],
  [/^(\d+) dakikadır ajandan olay yok$/, "No events from the agent for $1 min"],
  [/^`([\s\S]*)` komutu hata verdi$/, "`$1` failed"],
  [/^(\S+) adımının 1 sorusu var$/, "The $1 stage has a question"],
  [/^(\S+) adımının (\d+) sorusu var$/, "The $1 stage has $2 questions"],
  [/^Ajanın 1 sorusu var$/, "The agent has a question"],
  [/^Ajanın (\d+) sorusu var$/, "The agent has $1 questions"],
  [/^Niyet sapması: ajan 1 soru soruyor$/, "Intent gap: the agent asks a question"],
  [/^Niyet sapması: ajan (\d+) soru soruyor$/, "Intent gap: the agent asks $1 questions"],
  [/^Niyet sapması: (\S+) 1 soru soruyor$/, "Intent gap: $1 asks a question"],
  [/^Niyet sapması: (\S+) (\d+) soru soruyor$/, "Intent gap: $1 asks $2 questions"],
  [/^(\S+) adımı onayını bekliyor$/, "$1 stage waiting for approval"],
  [/^Korumalı dosya değişikliği: (.+)$/, "Protected file change: $1"],
  [/^PR'da CI kırmızı · PR'da 1 yeni yorum$/, "CI is red on the PR · 1 new comment on the PR"],
  [/^PR'da CI kırmızı · PR'da (\d+) yeni yorum$/, "CI is red on the PR · $1 new comments on the PR"],
  [/^PR'da 1 yeni yorum$/, "1 new comment on the PR"],
  [/^PR'da (\d+) yeni yorum$/, "$1 new comments on the PR"],
  [/^CI: (.*) başarısız$/, "CI: $1 failed"],
  [/^1 yeni hafıza önerisi$/, "1 new memory proposal"],
  [/^(\d+) yeni hafıza önerisi$/, "$1 new memory proposals"],
  [/^Hafıza dosyasında 1 sorun$/, "1 problem in the memory files"],
  [/^Hafıza dosyasında (\d+) sorun$/, "$1 problems in the memory files"],
  [/^1 yeni iş önerisi$/, "1 new work proposal"],
  [/^(\d+) yeni iş önerisi$/, "$1 new work proposals"],
  [/^Kota doldu: varsayılan giriş · (.+)$/, "Usage limit reached: default login · $1"],
  [/^Kota doldu: (.+) · (.+)$/, "Usage limit reached: $1 · $2"],
  [/^varsayılan giriş hesabının kullanım sınırı doldu \((.*)\); (.+?)'(?:de|da|te|ta) sıfırlanır\. Bekleyen ticket'lar o zaman kendiliğinden devam eder\.$/,
    "The default login reached its usage limit ($1); it resets at $2. Waiting tickets then continue by themselves."],
  [/^(.+?) hesabının kullanım sınırı doldu \((.*)\); (.+?)'(?:de|da|te|ta) sıfırlanır\. Bekleyen ticket'lar o zaman kendiliğinden devam eder\.$/,
    "$1 reached its usage limit ($2); it resets at $3. Waiting tickets then continue by themselves."],
  [/^Ajan bu komutun bitmesini bekliyor \((.*?)\): (.*)$/, "The agent is waiting for this command to finish ($1): $2"],
  [/^alttaki dilim squash\/rebase ile birleşti; bu dal onun eski commit'lerini taşıyor\. (.+?) üstüne yeniden düzenlenmeli \(git rebase --onto\)\.$/,
    "The slice below was merged with squash/rebase; this branch still carries its old commits. It should be rebased onto $1 (git rebase --onto)."],
  [/^alttaki dilim squash\/rebase ile birleşti; bu dal onun eski commit'lerini taşıyor\. (.+?) üstüne yeniden düzenlenmeli \(git rebase --onto\)\.\n\nSon deneme çakıştı: ([\s\S]*)$/,
    "The slice below was merged with squash/rebase; this branch still carries its old commits. It should be rebased onto $1 (git rebase --onto).\n\nThe last attempt conflicted: $2"],
  [/^(.+?) squash\/rebase ile birleşti; bu dal onun eski commit'lerini taşıyor\. (.+?) üstüne yeniden düzenlenmeli \(git rebase --onto\)\.$/,
    "$1 was merged with squash/rebase; this branch still carries its old commits. It should be rebased onto $2 (git rebase --onto)."],
  [/^(.+?) squash\/rebase ile birleşti; bu dal onun eski commit'lerini taşıyor\. (.+?) üstüne yeniden düzenlenmeli \(git rebase --onto\)\.\n\nSon deneme çakıştı: ([\s\S]*)$/,
    "$1 was merged with squash/rebase; this branch still carries its old commits. It should be rebased onto $2 (git rebase --onto).\n\nThe last attempt conflicted: $3"],
  [/^Alttaki dal \((.*?)\) yeniden düzenlendi; bu dal onun üstüne taşınmalı\.$/,
    "The branch below ($1) was rebased; this branch should be moved onto it."],
  [/^Alttaki dal \((.*?)\) yeniden düzenlendi; bu dal onun üstüne taşınmalı\.\n\nSon deneme çakıştı: ([\s\S]*)$/,
    "The branch below ($1) was rebased; this branch should be moved onto it.\n\nThe last attempt conflicted: $2"],
  [/^Son deneme çakıştı: ([\s\S]*)$/, "The last attempt conflicted: $1"],
  [/^(\S+): kapatılacak bir şey yok$/, "$1: nothing to close"],
  [/^(\S+): bu öğe bu arada değişti; sayfayı yenile$/, "$1: this item changed in the meantime; reload the page"],

  // dashboard.py
  [/^(.+): lead zaten çalışıyor$/, "$1: lead is already running"],

  // lead.py: proposals' reasons, the last run, its warnings, CI signals
  [/^Yol haritasının sıradaki maddesi \((.+)\)\.$/, "The next item on the roadmap ($1)."],
  [/^(\S+) review'unda ertelenmiş bulgu; hâlâ ticket'ı yok\.$/, "A finding deferred in $1's review; it has no ticket yet."],
  [/^oturum sonuç vermedi \((.*)\)$/, "the session gave no result ($1)"],
  [/^5 saatlik kota %(\d+)$/, "5-hour usage at $1%"],
  [/^yol haritası okunamadı: ([\s\S]*)$/, "roadmap unreadable: $1"],
  [/^CI okunamadı: ([\s\S]*)$/, "CI unreadable: $1"],
  [/^issue'lar okunamadı: ([\s\S]*)$/, "issues unreadable: $1"],
  [/^(.+) iş akışını (\S+) dalında yeşile döndür$/, "Turn the $1 workflow green on $2"],
  [/^(.+) iş akışı (\S+) dalında kırmızı$/, "The $1 workflow is red on $2"],
  [/^(.+), (\S+) dalında kırmızı: son (\d+) koşusu başarısız \(en son (\w*): (.*)\)\.$/,
    "$1 is red on $2: its last $3 runs failed (latest $4: $5)."],
  [/^Faz: (.+)$/, "Phase: $1"],
  [/^(.+) kontrollü: öneriler yalnız öneri; ticket'ı 'Ticket olarak aç' ile sen oluştur$/,
    "$1 is controlled: proposals are only proposals; open the ticket yourself with 'Open as ticket'"],

  // changes.py: the review page's range labels and file risk reasons
  [/^AI review, tur (\d+) sonrası$/, "After AI review, round $1"],
  [/^Codex review, tur (\d+) sonrası$/, "After Codex review, round $1"],
  [/^GitHub geri bildirimi, tur (\d+) sonrası$/, "After GitHub feedback, round $1"],
  [/^senin review'in, tur (\d+) sonrası$/, "After your review, round $1"],
  [/^AI review, tur (\d+)$/, "AI review, round $1"],
  [/^Codex review, tur (\d+)$/, "Codex review, round $1"],
  [/^GitHub geri bildirimi, tur (\d+)$/, "GitHub feedback, round $1"],
  [/^senin review'in, tur (\d+)$/, "your review, round $1"],
  [/^(.+) sonrası$/, "After $1"],
  [/^1 açık bulgu$/, "1 open finding"],
  [/^(\d+) açık bulgu$/, "$1 open findings"],
  [/^(\d+) satır$/, "$1 lines"],

  // memory.py: where the memory lives (hint) and what its files' parser warned about
  [/^(.+?)'ta bu repoya özel eşleme yok \(projects\.json\); üst klasörün eşlemesi \((.+)\) birden çok repoyu karıştıracağı için kullanılmıyor$/,
    "$1 has no mapping of its own for this repo (projects.json); the parent folder's mapping ($2) is not used because it would mix several repos"],
  [/^(.+?)'ta bu repoya özel eşleme yok \(projects\.json\); üst klasörün eşlemesindeki (.+) klasörü yok$/,
    "$1 has no mapping of its own for this repo (projects.json); the folder $2 of the parent folder's mapping does not exist"],
  [/^(.+?)'ta bu repoya özel eşleme yok \(projects\.json: vault_dir boş\)$/,
    "$1 has no mapping of its own for this repo (projects.json: vault_dir is empty)"],
  [/^(.+?)'ta bu repoya özel eşleme yok \(projects\.json\)$/, "$1 has no mapping of its own for this repo (projects.json)"],
  [/^(.+?)'ta (.+) klasörü yok \(projects\.json\)$/, "$1 has no folder $2 (projects.json)"],
  [/^(.+?) eşlemesi okunamadı \((.+?)\): ([\s\S]*)$/, "$1 mapping unreadable ($2): $3"],
  [/^(.+?): frontmatter kapanmamış \(--- satırı yok\)$/, "$1: frontmatter not closed (no --- line)"],
  [/^(.+?): başlık okunamadı: (.*)$/, "$1: unreadable heading: $2"],
  [/^(.+?): (M-\d+) '(.*)' yazıyor ama (\S+) (\S+) dosyası; (\S+) sayıldı$/, "$1: $2 says '$3' but $4 is the $5 file; counted as $6"],
  [/^(.+?): (\S+) (\S+) içinde de var; ilki kullanılıyor$/, "$1: $2 is also in $3; the first one is used"],
  [/^(.+?): (\S+) başlıksız$/, "$1: $2 has no title"],

  // roadmap.py: roadmap.md / BMad warnings, scope-change candidates, refusals
  [/^(.+?): evre başlığı okunamadı: (.*)$/, "$1: unreadable phase heading: $2"],
  [/^(.+?): madde bir evrenin \(## P1 · başlık\) altında değil: (.*)$/, "$1: item not under a phase (## P1 · title): $2"],
  [/^(.+?): maddenin kimliği yok \(R-<n>\): (.*)$/, "$1: item has no id (R-<n>): $2"],
  [/^(.+?): (\S+) durumu okunamadı: (.*) \(aktif ya da bitti\)$/, "$1: unreadable status of $2: $3 (aktif or bitti)"],
  [/^(.+?): (\S+) durumu okunamadı: (.*)$/, "$1: unreadable status of $2: $3"],
  [/^epics\.md:(\d+): Story (\S+) Epic (\S+) altında$/, "epics.md:$1: Story $2 is under Epic $3"],
  [/^sprint-status\.yaml: (\S+) epics\.md'de yok$/, "sprint-status.yaml: $1 is not in epics.md"],
  [/^sprint-status\.yaml: (\S+): bilinmeyen durum '(.*)'; yapılacak sayıldı$/, "sprint-status.yaml: $1: unknown status '$2'; counted as todo"],
  [/^(.+?): evre yok \(## P1 · başlık\)$/, "$1: no phases (## P1 · title)"],
  [/^(.+?): okunamadı: ([\s\S]*)$/, "$1: unreadable: $2"],
  [/^(\S+) evresine ekle: (.*)$/, "Add to $1: $2"],
  [/^(\S+) çıkar$/, "Remove $1"],
  [/^Yeni evre: (.*)$/, "New phase: $1"],
  [/^(\S+) yok; önce bir yol haritası kaydet$/, "$1 does not exist; save a roadmap first"],
  [/^(\S+) yol haritasında yok$/, "$1 is not in the roadmap"],

  // onboard_agent.py / onboard_verify.py: onboarding progress
  [/^kontrol (\d+)\/(\d+): (.*)$/, "check $1/$2: $3"],
  [/^denendi · (\d+) sn$/, "tried · $1 s"],
  [/^geçmedi: (.*)$/, "failed: $1"],

  // protected.py, ticket.py: refusals
  [/^(.+) bu yama hazırlandıktan sonra başka bir değişiklikle değişti; yama uygulanamıyor\. Reddet ve ajanın yeni bir yama hazırlamasını iste\.$/,
    "$1 changed in another change after this patch was prepared; the patch cannot be applied. Reject it and ask the agent to prepare a new one."],
  [/^bilinmeyen tip `(.*)`; geçerli tipler: (.*)$/, "unknown type `$1`; valid types: $2"],

  // weekly.py: a report saved in Turkish, line by line (the server renders it in English when asked with ?lang=en)
  [/^# Haftalık rapor · (.+)$/, "# Weekly report · $1"],
  [/^- Biten: \*\*(\d+)\*\* · birleşen PR: \*\*(\d+)\*\* · başarısız: \*\*(\d+)\*\*$/, "- Done: **$1** · merged PRs: **$2** · failed: **$3**"],
  [/^- Maliyet: \*\*\$([\d.]+)\*\* \((\d+) oturum\)$/, "- Cost: **$$1** ($2 sessions)"],
  [/^- Teslim: istek olmadan birleşen %(\S+) · PR başına tur (\S+)$/, "- Delivery: merged without a change request $1% · rounds per PR $2"],
  [/^- Teslim: istek olmadan birleşen %(\S+)$/, "- Delivery: merged without a change request $1%"],
  [/^- Şu an seni bekleyen: \*\*(\d+)\*\* · review'a hazır: \*\*(\d+)\*\*$/, "- Waiting on you now: **$1** · ready for review: **$2**"],
  [/^- … ve (\d+) tane daha$/, "- … and $1 more"],
  [/^- (.+): (\d+) öneri onay bekliyor, (\d+) kayıt budanabilir$/, "- $1: $2 proposals to approve, $3 entries to prune"],

  // Families whose Turkish words sit inside the variable parts: one pattern per word, built here.
  ...(() => {
    const out = [];
    // host.py: the memory words (host.WORDS) in the panel's chip, a stall's title and its details' Makine line.
    const memory = [["normal", "normal"], ["baskı altında", "under pressure"], ["kritik", "critical"], ["bilinmiyor", "unknown"]];
    for (const [tr, en] of memory) {
      out.push([new RegExp(`^Bellek: ${tr} · swap %(\\d+)$`), `Memory: ${en} · swap $1%`]);
      out.push([new RegExp(`^Bellek: ${tr}$`), `Memory: ${en}`]);
      out.push([new RegExp(`^Makine: Bellek: ${tr} · swap %(\\d+) · yük ([\\d.]+)$`), `Machine: memory ${en} · swap $1% · load $2`]);
      out.push([new RegExp(`^Makine: Bellek: ${tr} · swap %(\\d+)$`), `Machine: memory ${en} · swap $1%`]);
      out.push([new RegExp(`^Makine: Bellek: ${tr} · yük ([\\d.]+)$`), `Machine: memory ${en} · load $1`]);
      out.push([new RegExp(`^Makine: Bellek: ${tr}$`), `Machine: memory ${en}`]);
      // a stall's whole details: the command (it may span lines), the machine, the note
      const head = "^Ajan bu komutun bitmesini bekliyor \\((.*?)\\): ([\\s\\S]*?)\\nMakine: Bellek: " + tr;
      const tail = "\\nZiggy komutu durdurmaz: takıldıysa ajana yön ver ya da durdur\\.$";
      const lead = "The agent is waiting for this command to finish ($1): $2\nMachine: memory " + en;
      const note = "\nZiggy does not stop the command: if it is stuck, steer the agent or stop it.";
      out.push([new RegExp(`${head} · swap %(\\d+) · yük ([\\d.]+)${tail}`), `${lead} · swap $3% · load $4${note}`]);
      out.push([new RegExp(`${head} · swap %(\\d+)${tail}`), `${lead} · swap $3%${note}`]);
      out.push([new RegExp(`${head} · yük ([\\d.]+)${tail}`), `${lead} · load $3${note}`]);
      out.push([new RegExp(`${head}${tail}`), `${lead}${note}`]);
    }
    // attention.py: a stall's title, "Komut 12 dk’dır sürüyor: <komut>" and what host.suffix adds.
    const stall = "^Komut (\\d+) dk’dır sürüyor: ([\\s\\S]*?)";
    for (const [tr, en] of [["kritik", "critical"], ["baskı altında", "under pressure"]]) {
      out.push([new RegExp(`${stall} · bellek ${tr} · swap %(\\d+)$`), `Command running for $1 min: $2 · memory ${en} · swap $3%`]);
      out.push([new RegExp(`${stall} · bellek ${tr}$`), `Command running for $1 min: $2 · memory ${en}`]);
    }
    out.push([new RegExp(`${stall} · swap %(\\d+)$`), "Command running for $1 min: $2 · swap $3%"]);
    out.push([new RegExp(`${stall}$`), "Command running for $1 min: $2"]);
    // attention.py: "Bekliyor ama <dependency> <why>" and its details, per stack.dependency reason (one dependency).
    const why = [["başarısız oldu", "failed", 0], ["PR'ı birleşmeden kapandı", "had its PR closed without merging", 0],
      ["bitmeden arşivlendi", "was archived before it finished", 0], ["bulunamıyor", "cannot be found", 0],
      ["dilimi (\\S+) takıldı", "is stuck on its slice $", 1], ["takıldı", "is stuck", 0]];
    const then = [["bitince başlatmak için sorulur.", "once it is done, you are asked to start this one."],
      ["bitince bu ticket kendiliğinden başlar.", "once it is done, this ticket starts by itself."]];
    for (const [tr, en, groups] of why) {
      const said = (n) => (groups ? en.replace("$", "$" + n) : en);
      out.push([new RegExp(`^Bekliyor ama (\\S+) ${tr}$`), `Waiting, but $1 ${said(2)}`]);
      for (const [trThen, enThen] of then) {
        out.push([new RegExp(`^(\\S+) başlamadı: bağımlı olduğu (\\S+) ${tr}\\. Onu düzelt \\(yeniden koştur ya da yeni ticket aç\\); `
          + trThen.replace(/\./g, "\\.") + "$"),
        `$1 has not started: $2, which it depends on, ${said(3)}. Fix it (rerun it or open a new ticket); ${enThen}`]);
      }
    }
    // attention.py: a dismissal refused because the item waits for an action: "<id>: “<LABEL>” kapatılamaz, …"
    const labels = {
      "Ajanın soruları var": "The agent has questions", "Plan onayını bekliyor": "Plan waiting for approval",
      "Bitti, PR açılmadı": "Done, no PR yet", "Koşu başarısız oldu": "Run failed", "Komut hata verdi": "Command failed",
      "Ticket state'i okunamıyor": "Ticket state unreadable", "Dal PR'dan sonra değişti": "Branch changed after the PR",
      "PR kapandı ama worktree'de iş kaldı": "PR closed but the worktree still has work",
      "Oturum bir süredir sessiz": "Session quiet for a while", "Komut uzun sürüyor": "A command is taking long",
      "Koşunun süreci ölmüş": "The run's process died", "Yeni hafıza önerisi": "New memory proposal",
      "Beklediği ticket takıldı": "A ticket it waits for is stuck", "Yeniden düzenleme gerekiyor": "Needs a restack",
      "Başlatılmayı bekliyor": "Waiting to be started", "Kota doldu": "Usage limit reached",
      "Yeni iş önerisi": "New work proposal", "Korumalı dosya değişikliği": "Protected file change",
    };
    for (const [tr, en] of Object.entries(labels)) {
      out.push([new RegExp(`^(\\S+): “${tr}” kapatılamaz, bir işlem bekliyor$`), `$1: “${en}” cannot be closed; it waits for an action`]);
    }
    out.push([/^(\S+): “(.+)” kapatılamaz, bir işlem bekliyor$/, "$1: “$2” cannot be closed; it waits for an action"]);
    return out;
  })(),
]);
