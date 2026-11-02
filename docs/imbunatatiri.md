# Age of AI — Îmbunătățiri și planuri

**Actualizare ritm, 1 noiembrie 2026:** la cererea utilizatorului, modul de civilizație folosește acum normal **5 minute reale = 1 oră de joc (12×)**. Încetinește adaptiv când deciziile AI întârzie, elimină răspunsurile învechite și arată viteza efectivă. Modificările nu au fost rulate sau testate, pentru a nu concura cu celălalt proiect al utilizatorului.

**1 noiembrie 2026 — extindere nouă:** primul mod de civilizație continuă este separat de lumea clasică. Alegeri confirmate: orașe și descoperiri păstrate, mai multe civilizații, conducător AI cu birouri distincte pentru economie/cercetare/guvernare, oameni reali în funcții care pot rămâne sau fi înlocuiți, tradiții fictive, 1 minut real = 1 oră de joc. Implementarea inițială și limitele ei sunt în [civilization-development.md](civilization-development.md); companii, piața muncii și politica detaliată rămân etape viitoare.

Acest document adună ce s-a îmbunătățit după versiunile 1–7. Documentul de design rămâne referința pentru reguli; aici sunt propunerile și ce s-a construit din ele. **Construite: secțiunile 1, 2, 3, 6, 7 (bandiții și Minunea), vocea din 5, apoi 8–11 (revizia de cod, rivalitatea, învățarea).** Rămân pentru tine: grafica (5), antrenamentul PPO (11) și commit-urile (12).

**Starea de azi (31 octombrie 2026):** lumea live a rulat pe acest PC în timp real, cu Nordul pe `qwen3:8b` și Sudul pe `gemma3:12b`; acum e oprită (`scripts/stop_world.ps1`) și pornește din ultima salvare cu `scripts/start_world.ps1`. Toate cele 260 de teste rapide trec. Nimic nu e salvat încă în git.

## 1. Armata: atac, apărare și îmbunătățiri — ✅ construit

**Stare:** totul din această secțiune este în cod, cu teste (tests/test\_army\_plan.py). Numerele stau în rules.yaml (people, army, military\_upgrades, genome). În plus față de plan, limita de ferme a crescut de la 10 la 40, altfel 300 de oameni nu pot fi hrăniți. Ce s-a ales la construire e la sfârșitul secțiunii.

**Ce vrei (răspunsul tău):**

- **Maximum 300 pe castel, muncitori și soldați împreună.** Fiecare civilizație poate avea cel mult 300 de oameni: cei care muncesc plus cei din armată.
- **Armata:** arcași, cavaleri și spadasini.
- **Îmbunătățiri:** uniforme (armură) și arme mai bune.
- **Cea mai bună logică:** când există o armată, cele două AI-uri atacă, se apără și își îmbunătățesc armata singure, cât mai inteligent.

**Ce există deja (versiunea 5):**

- Unitățile: miliție, sulițaș, arcaș, spadasin, cavaler și berbec, cu statisticile din documentul de design.
- AI-ul atacă singur când armata lui este mai puternică decât a vecinului cu pragul θ\_atac (gena 1,0 – 3,0), după legea lui Lanchester. Se retrage când îi rămâne sub 35% din viață.
- Soldații de acasă apără satul: atacă orice inamic care se apropie la 8 pătrățele, iar turnurile de pază trag.
- Fierăria face îmbunătățiri militare: Arme (+1 atac) și Armuri (+1 armură), câte 3 niveluri, de la castelul 5.
- AI-ul alege tipul de soldat după raportul spionului (de exemplu sulițași contra cavalerilor).

**Ce trebuie schimbat:**

| Schimbare | Cum | Efect |
| --- | --- | --- |
| **300 pe castel, muncitori + soldați** | Limita de 200 de oameni devine 300 pe castel, împărțită între muncitori și soldați. AI-ul alege împărțirea cu gena raport muncitori/soldați. | Armate mari, dar fiecare soldat e un muncitor în minus. |
| **Mai mulți muncitori** | Limita de acum, W\_max = 2 + 2L (cel mult 20), e prea mică pentru o armată mare. Propunere: W\_max crește cu castelul până la circa 100 la castelul 10. | Economia poate hrăni armata (vezi costul mai jos). |
| **Uniforme și arme pe 5 niveluri** | Armele și Armurile de la Fierărie urcă la 5 niveluri în loc de 3, cu același cost crescător. Pe hartă, uniforma soldaților se schimbă la fiecare nivel. | Armata se vede mai puternică și chiar este. |
| **Promovarea soldaților** | Un soldat existent poate fi făcut un tip mai bun plătind diferența de cost (miliție → spadasin, sulițaș → cavaler), la cazarmă, în jumătate din timpul de antrenament. | Armata crește în calitate, nu doar în număr. |
| **Veterani** | Un soldat care supraviețuiește luptelor câștigă experiență: +10% atac la 5 victime, +20% la 15. | AI-ul învață să-și păstreze soldații. |

**Cea mai bună logică de luptă (ce trebuie adăugat):**

| Ce face AI-ul | Cum |
| --- | --- |
| **Formație** | Spadasinii în față, arcașii la 3 pătrățele în spatele lor, cavalerii pe flancuri. |
| **Fiecare unitate își alege ținta potrivită** | Cavalerii merg după arcași, sulițașii după cavaleri, arcașii după sulițași (bonusurile din tabel). Toți lovesc ținta cea mai slăbită din rază, ca să o termine. |
| **Armata pleacă împreună** | Înainte de atac, soldații se adună la marginea satului și pleacă toți odată, nu câte unul. |
| **Răniții se retrag** | Un soldat sub 25% din viață se retrage acasă să se vindece, în loc să moară degeaba. |
| **Apărare din timp** | Când turnurile sau spionii văd o armată venind, soldații se adună la castel înainte de atac. |
| **Contraatac** | După o apărare reușită, dacă armata inamicului a pierdut mult, AI-ul atacă imediat înapoi. |
| **Asediu** | Când castelul inamic are ziduri sau turnuri, AI-ul aduce berbeci și îi apără pe drum. |

Toate aceste alegeri au gene (de exemplu cât de repede se retrag răniții), ca evoluția de noapte să găsească singură cea mai bună logică.

**Cum s-a construit (alegeri):**

- **Limita de 300:** toți sătenii (muncitori, copii, bătrâni) plus soldații ≤ 300 pe castel. Soldații dorm în cazarmă, deci nu au nevoie de paturi în case.
- **Muncitori pe nivel de castel:** 4, 6, 8, 14, 22, 32, 44, 58, 76, 100 (primele trei rămân ca în documentul de design, ca începutul jocului să nu se schimbe).
- **Promovări:** miliție → spadasin (la cazarmă, de la castelul 7) și sulițaș → cavaler (la grajd). Se promovează cel mai experimentat soldat aflat acasă; își păstrează victimele.
- **Adunarea:** armata se strânge la 8 pătrățele de castel, spre inamic, și pleacă când au sosit 80% (sau după 30 de minute). Relația R scade abia când armata pleacă.
- **Formația:** cel mai avansat soldat de corp la corp este „linia frontului”; arcașii stau la 3 pătrățele în spatele lui, cavalerii la 4 pătrățele în lateral.
- **Răniții:** gena nouă retragere (0,1 – 0,5, implicit 0,25). Armata întreagă se retrage doar dacă e mai slabă decât apărătorii rămași; dacă nu mai are cine să o oprească, merge mai departe spre castel.
- **Apărarea din timp:** o armată inamică la 30 de pătrățele de castel îi scoate pe apărători în întâmpinare.
- **Contraatacul:** dacă atacatorul a pierdut cel puțin jumătate din armată și apărătorul mai puțin, apărătorul are 2 ore în care poate contraataca (gena nouă contraatac, 0 – 1, implicit 0,5).
- **Asediul:** de la castelul 8, cu cel puțin 10 soldați, AI-ul face Atelierul de asediu și ia 2 berbeci la atac.
- **Uniformele pe hartă:** coiful arată nivelul Armurilor, iar arma nivelul Armelor: piele, bronz, fier, oțel, aur.

**Ce costă o armată mare:** fiecare soldat mănâncă 48 hrană/zi. De exemplu, 100 de muncitori și 200 de soldați înseamnă 300 × 48 = 14.400 hrană/zi. Un fermier cu unelte de oțel, Plug și Moară de apă face în jur de 250–300 hrană/zi, deci ar fi nevoie de circa 50–60 de fermieri. Cu limita de acum, de 20 de muncitori, o asemenea armată moare de foame; de aceea limita de muncitori trebuie să crească odată cu cea de 300.

## 2. Partidele de antrenament sunt prea scurte pentru război — ✅ construit

Civilizațiile ajungeau la castelul 3 în jurul zilei 40 și la castelul 4 (Cazarma) după ziua 90, dar partidele de noapte durau 60 de zile. Evoluția nu vedea niciodată armate, războaie sau spioni.

**Ce s-a făcut:**

- **Partide de 120 de zile** (în loc de 60). Cu taxa nouă (punctul 3), castelul 4 vine în jurul zilei 50, deci armatele apar în fiecare partidă. O noapte durează cam de două ori mai mult (în jur de 30 de minute pe 6 nuclee), tot mult sub fereastra de noapte a documentului.
- **Partide pornite la jumătatea jocului:** 2 din cele 8 runde ale nopții pornesc dintr-o lume salvată la castelul 4, jucată de genomul implicit (4 lumi, una pe anotimp). Genomurile nopții preiau apoi ambele civilizații pentru 60 de zile. Lumile se fac o singură dată (câteva minute, prima noapte) și se păstrează în data/scenarios; când se schimbă rules.yaml, se fac din nou. La început, ambele civilizații sunt identice, deci partida rămâne corectă.
- **Scorul pentru resurse adunate** crește cu lungimea partidei: E\_max = 1000 × zile (60.000 la 60 de zile, ca înainte). În partidele de la jumătate se numără doar ce a adunat genomul însuși.

## 3. Monedele frânează tot jocul — ✅ construit

Taxa era de 1 monedă/zi pe locuitor, iar castelul 2 cere 100 de monede și castelul 5 cere 491.

**Ce s-a făcut:** taxa este acum **2 monede/zi pe locuitor** (soldații plătesc și ei). Verificat în partide scurte: castelul 4 vine în ziua 46–59 în loc de după ziua 90, iar satul are 14 oameni în ziua 60 în loc de 8. Un soldat costă tot 1 monedă/zi solda, deci o armată se poate plăti.

## 4. De verificat la prima pornire — în mare parte ✅

- **Imaginile — ✅ verificat:** `python cli.py pictures` citește din ComfyUI lista de noduri și modele și confirmă că fluxul Flux 3 Klein are tot ce-i trebuie (verificat: totul e în regulă). `python cli.py pictures --test` desenează și un portret de probă (folosește placa video, deci îl pornești tu).
- **Harta — ✅ văzută:** jocul a rulat pe ecran (test ×60 și lumea reală). Au ieșit și s-au reparat: ceața arăta tot (acum fiecare regat vede doar ce a explorat, pe hartă și pe minihartă), eticheta „1 h 60 min”, timpul înghețat la prânz („paused: lunch break”), scripturile vechi din cache-ul browserului. Soldații, uniformele și bandiții apar abia după castelul 4, deci încă nu au fost văzuți în lumea reală.

## 5. Vocea și grafica

- **Vocea — ✅ instalată (cu acordul tău):** Piper (22 MB, de pe GitHub) și vocea engleză `en_US-lessac-medium` (63 MB, de pe Hugging Face) stau în `tools/piper` (ignorat de git). `rules.yaml` → `commentator.piper` și `piper_voice` le folosesc; radioul vorbește cu ele. Testele nu le pornesc niciodată.
- **Grafica:** totul e desenat din cod, cu forme simple. Seturi gratuite de imagini medievale CC0 (de exemplu Kenney) ar face harta să arate ca un joc adevărat.

## 6. Calitatea antrenamentului (țintele versiunilor 2 și 3) — ✅ construit

- **Testul fix are 16 partide** (4 pe anotimp, în loc de 2) și se joacă tot cu 120 de zile. Fiecare noapte salvează setările testului, iar „crește 5 nopți la rând” se numără doar între nopți cu aceleași setări (cele 8 nopți vechi, de 60 de zile, nu se amestecă cu cele noi).
- **Regulile sunt judecate pe mai multe nopți:** pragurile documentului rămân, scrise ca șansă de noroc: probă la ceva la fel de convingător ca 6 din 8 (14,5%), acceptare la ceva la fel de convingător ca 12 din 16 (3,8%). O regulă care câștigă măcar jumătate primește a doua noapte de test, iar una în probă care câștigă 60% poate continua până la 32 de partide.
- **Regulile văd armata:** variabilele soldați, puterea mea, puterea inamicului, relația și încrederea au acum valorile reale (înainte erau mereu 0). Ghidul pentru AI-ul care scrie reguli are intervalele noi (partide de 120 de zile, taxa de 2).

## 7. Mărunțișuri

- **Tabăra de bandiți — ✅ construit:** dintre locurile de pe linia egal depărtată de castele, tabăra îl alege pe cel cu **drumul de mers** cel mai egal până la ambele castele (de exemplu 82,3 și 82,0 pătrățele), nu doar distanța în linie dreaptă.
- **După Regat — ✅ construit (alegerea ta: Minunea, apoi o eră nouă):** castelul 10 deblochează o Minune (20.000 lemn, 30.000 piatră, 4.000 minereu, 3.000 aur, 5.000 monede; cam 12 zile cu 5 constructori). Materialele vin pe șantier pe măsură ce crește, ca la castel. Șantierul poate fi atacat și e atât de rezistent cât a crescut; distrus, dă 30% din ce s-a livrat. Prima Minune terminată câștigă era; în lumea live, după 7 zile începe o eră nouă pe o hartă nouă, cu ambele regate renăscute și istoria păstrată. Bara de sus arată era.
- **Git:** vezi secțiunea 12.

## 8. Revizia de cod — ✅ reparat

O citire a întregului proiect a găsit bug-uri, scurtături periculoase și cod dublat. Toate au teste noi (`tests/test_fixes.py`, `tests/test_after_castle_10.py`).

- **Șase bug-uri:** după întoarcerea la o salvare, ceața, bandiții și planificatorul de drumuri rămâneau legați de o copie veche a lumii (id-uri duplicate); memoria regilor chema Ollama din firul jocului și îl bloca; gena de cinste se arunca din nou la fiecare 5 minute, deci tratatele se rupeau într-o oră (acum o dată pe tratat); un regat rămas fără oameni nu mai renăștea; curățenia „disc plin” nu ștergea nimic; arcașii plecau la război, iar soldații de corp la corp rămâneau acasă.
- **Cinci mai mici:** lista „în folosință” din Evoluție, recompensa pentru bandiți după ce jefuiseră castelul (acum bunurile se întorc dacă sunt prinși pe drum), limite pe cererile paginii, portretele legate de persoană (nu de nume), târgurile cu un regat căzut.
- **Pagina:** ceața pe regat, eticheta „Farm - 87% - 2 h”, pauza de prânz și de noapte, scripturile încărcate mereu proaspete.
- **Cod dublat** înlocuit cu funcții comune: mersul pe drum, numărarea unităților, cea mai apropiată căsuță liberă, datele spionilor, cercetașilor și soldaților.

## 9. Rivalitatea: două minți diferite în lumea live — ✅ construit

Ce ai cerut: fără meciuri aranjate; fiecare regat gândește cu modelul lui, iar războaiele vin din deciziile regilor și din diplomația care se strică.

- **Un model pe regat:** `rules.yaml` → `llm.models: {north: qwen3:8b, south: gemma3:12b}` (încap împreună pe placa de 16 GB). Sfatul, diplomatul și reflecția fiecărui regat folosesc modelul lui; radioul și Cronica folosesc modelul comun (`qwen3:8b`).
- **Regele decide războiul și pacea:** planul lui are „atac”, „apărare”, „cere pace” sau „nimic”, cu țintă și motiv, în regulile jocului (14 zile de pace la început, minimum 5 soldați, tratatul se poate rupe cu prețul încrederii).
- **Diplomația în faptele regelui:** relația, încrederea în ambele sensuri, tratatul, tributurile refuzate, promisiunile încălcate, spionii prinși, atacurile.
- **O linie de genomuri pe regat:** antrenamentul de noapte ține jumătate din populație pentru fiecare regat; genomurile Nordului joacă împotriva celor ale Sudului. Dimineața fiecare regat primește cel mai bun din linia lui.
- **Modelul își alege singur caracterul** (`llm.mind: mixed`): la naștere și o dată pe săptămână, modelul vede situația, ce i s-a întâmplat și cele 20 de gene (intervalul și ce schimbă fiecare) și alege valori noi, cu un motiv. Codul păstrează fiecare genă în interval și o mută cel mult 25% din interval, cel mult 6 gene o dată. Primele alegeri: Nordul (qwen3:8b) a crescut uneltele, piatra, prudența, diplomația și cinstea; Sudul (gemma3:12b) a crescut casele, pregătirea de iarnă, apărarea și soldații.
- **Fapte, nu sfaturi:** modelele primesc doar regulile și faptele lumii (costuri, ce face fiecare acțiune, limite, ce s-a întâmplat), fără „construiește întâi ferme” și fără „ești curajos”. Învață din consecințe: memorii, lecțiile nopții, tabela.
- **Tabela rivalității** (tabul Diplomație): războaie începute și câștigate, bătălii, castele distruse, căderi, Minuni și ere câștigate, cel mai bun castel; pe regat și pe model.
- **Costul fiecărui model:** apeluri, eșecuri, tokeni la intrare și la ieșire, timp de gândire, pe zi (tabelul `model_usage`). Pregătit pentru modele plătite (ChatGPT, Claude): un client nou are nevoie doar de interfața `LLM` din `ai/llm.py`.
- **Două minți de la început:** fără antrenament, Nordul pornește ca **constructor prudent**, iar Sudul ca **războinic agresiv** (`genome.live_start`).

## 10. Învățare și predicție — ✅ construit

Ideea ta: jocul e matematic, fără instrucțiuni despre cum se joacă; AI-ul învață din necesitate.

- **Simularea viitorului (Monte Carlo):** când genele spun „atac”, lumea live se copiază și joacă următoarele 72 de ore în viteză, o dată cu atacul și o dată fără, de câte două ori cu noroc diferit, într-un proces separat (cam 8 secunde; lumea nu așteaptă). Armata pleacă doar dacă atacul a ieșit mai bine, după scorul antrenamentului (castel, strâns, oameni, supraviețuire; al nostru minus al inamicului). Prima concluzie a matematicii: un raid cu o duzină de soldați asupra unui sat de castel 1 nu merită, pentru că soldații abia zgârie zidurile de piatră și doar berbecii le dărâmă.
- **CMA-ES în antrenamentul de noapte** (`evolution.method: cmaes`): fiecare linie învață ce direcții ale genelor merg, din clasamentul nopții, și trage din ele genomurile nopții următoare; cel mai bun se păstrează. Starea rămâne în baza de date, deci învață noapte de noapte. Vechiul algoritm genetic rămâne disponibil (`ga`).

## 11. Rețea neuronală învățată prin recompensă (PPO) — ✅ construit, ⏳ de antrenat

- **Ce vede:** 49 de numere (anotimp, castel, oameni, soldați, resurse față de depozit, clădiri, unelte, ce se știe despre vecin, diplomația).
- **Ce decide, la fiecare 6 ore de joc:** cât contează 9 domenii (0,5×, 1× sau 2×) și război, apărare, pace sau nimic. E același plan pe care îl dă regele LLM; formulele fac munca zilnică.
- **Cum învață:** doar din recompensă: schimbarea scorului jocului (al nostru minus al inamicului), +1 la victorie, −1 la înfrângere sau dacă regatul cade. Joacă 6 partide de 60 de zile în paralel, contra genomului standard și a campionilor.
- **Unde:** antrenamentul rulează în mediul separat Python 3.12 + PyTorch (`.venv-rl`, pe procesor, 740 MB pe disc, ignorat de git). Rețeaua antrenată se salvează în `data/rl/policy.json`, iar jocul (Python 3.15) o rulează în Python simplu, fără PyTorch, cu aceleași răspunsuri.
- **Pornire:** `.venv-rl\Scripts\python -m rl.train_ppo` (Ctrl+C oprește; data viitoare continuă). Progresul: `data/rl/train_log.csv`. Ca rețeaua să joace un regat: `rules.yaml` → `rl.civs: [south]`.
- **De așteptat:** rețeaua pornește de la zero și joacă la întâmplare. Primele rezultate utile apar după o zi-două de antrenament, iar bune după câteva zile.

## 12. Rămase și idei

- **Grafica CC0** (secțiunea 5): trebuie ales un pachet de imagini, iar harta redesenată pentru el.
- **Git — când spui tu:** încă nu există niciun commit; tot proiectul stă doar în folder.
- **Modele plătite (ChatGPT, Claude) cu buget mic:** totul e pregătit (un model pe regat, numărarea tokenilor). Pentru cost minim: doar regele în cloud, întrebat doar la evenimente (atac, foamete, castel nou, mesaj, caracterul săptămânal), cu un plafon de buget după care regatul trece pe modelul local. Cheile stau în variabile de mediu Windows, nu în fișiere.
- **Oameni care gândesc:** fiecare om cu memoria lui și cu un model local la momentele importante (ce face seara, grevă sau fugă, prietenii, căsătorie, doliu), cu un plafon de gânduri pe zi. Corpul rămâne pe formule, ca jocul să rămână rapid.
