# VocalFlash Bot — verifica idempotenza e isolamento MultiVocale

**Data verifica:** 2026-10-25  
**Branch esaminato:** `assistant-conversation-photo-v1`  
**Tipo di verifica:** lettura statica dei file e dei test esistenti; nessun test locale eseguito in questa verifica, nessun invio WhatsApp reale, nessuna scrittura sul database live.

## Risultati confermati

### Già implementato e coperto da test esistenti

- Il runtime MultiVocale conserva lo stato dei batch in Redis e gli oggetti audio in storage privato temporaneo.
- La creazione della raccolta in coda usa un lock Redis; esiste un test concorrente che verifica il riuso dello stesso batch.
- Il runtime usa lease per il processamento e impedisce a una lease obsoleta di finalizzare il batch recuperato da un altro worker.
- Dopo un tentativo di invio con esito ambiguo, il runtime non propone un retry cieco e richiede riconciliazione.
- Il ponte testuale usa un marker Redis per evitare un secondo invio della risposta dello stesso messaggio; i test coprono replay e timeout ambiguo.
- Il workflow Python della branch include i test del bridge, stato, storage e runtime/timing.

Evidenza CI disponibile: [suite bot riuscita](https://github.com/VocalFlash/vocalflash-bot/actions/runs/40121561641). È evidenza del commit e della suite eseguita in quel run, non prova di test reali Meta né del comportamento su infrastruttura live.

## Rischio da risolvere prima di dichiarare completato l'isolamento multi-account

Nella versione letta, le chiavi Redis di stato sono costruite principalmente usando il solo identificativo del mittente:
- `MultivocaleStateStore.active_key(sender)`;
- `PersistentMultivocaleRuntime._last_key(sender)`;
- `PersistentMultivocaleRuntime._queued_key(sender)`.

La chiave del coordinatore e la coda globale dei batch sono invece condivise intenzionalmente dal runtime. Gli ID batch sono UUID, ma questo non risolve da solo l'isolamento delle chiavi attive, dell'ultimo audio e della raccolta in coda.

**Impatto potenziale:** se la stessa applicazione Redis serve più numeri/account WhatsApp e lo stesso mittente interagisce con più account, lo stato indicizzato solo per mittente potrebbe essere condiviso fra contesti. La configurazione attuale potrebbe essere single-account; questa verifica non dimostra che la collisione si sia verificata in produzione. Va trattato come un rischio architetturale da testare, non come un incidente confermato.

### Gate prima della correzione

1. Verificare se il deployment è effettivamente single-account oppure multi-account e se Redis è condiviso tra numeri WhatsApp.
2. Definire un identificatore di namespace stabile, ricavato dal contesto account/canale configurato e non da dati forniti liberamente dal mittente.
3. Aggiungere test di collisione: stesso mittente in due account deve avere batch attivi, ultimo audio e coda indipendenti; stesso mittente nello stesso account deve continuare a deduplicare.
4. Se necessario, migrare con compatibilità esplicita le chiavi Redis esistenti. Non cambiare il formato delle chiavi senza una strategia di transizione, perché si rischia di perdere accesso ai batch pendenti.
5. Rieseguire l'intera suite Python e i test di recupero/restart; aggiornare la matrice con risultati osservati.
6. Solo dopo valutare l'integrazione audio con Assistant API. Non modificare ora il routing audio, il timing MultiVocale o i dati live.

## Altri limiti della verifica

- I test fake non dimostrano il comportamento reale del provider Meta in caso di timeout.
- Il marker di risposta del bridge ha una TTL di 24 ore: la deduplicazione non è una garanzia permanente oltre tale periodo.
- Il normale testo customer viene inoltrato all'Assistant API; l'audio resta nel percorso MultiVocale/legacy e non è ancora integrato nel contratto audio Assistant API.
- La CI verde non sostituisce test d'integrazione isolati, verifica del namespace account, restart test e riconciliazione del database.

## Stato

- **Già fatto/testato:** guardie locali per dedup/invio, lease e batch concorrenti coperti dai test esistenti; CI della branch verificata come verde per il run citato.
- **Progettato:** contratto audio → Assistant API e guardie conservative documentate nel repository JavaScript.
- **Da fare:** confermare il modello single-account/multi-account, testare e correggere se necessario lo scoping Redis, eseguire test di restart reali in ambiente isolato, completare l'integrazione audio solo dopo i gate.
- **Non autorizzato da questa verifica:** merge, deploy, modifica di schema live, invii WhatsApp reali.


## Aggiornamento operativo 2026-10-25 — namespace Redis implementato sul branch isolato

### Implementato, non ancora testato sul nuovo head
- Branch assistant-conversation-photo-v1, head candidato 197f610d271b92374566080e4f748bdae7fde06c.
- Aggiunto uno scope Redis opaco derivato da RENDER_SERVICE_ID e WA_PHONE_ID.
- Il runtime MultiVocale usa lo scope per batch attivi, ultimo audio, raccolte in coda, due queue, cleanup queue e lock del coordinator.
- Sullo stesso branch sono stati scoped anche deduplica webhook, lock mittente e marker di tentativo risposta dell'Assistant.
- Aggiunti test unitari per stesso mittente in namespace diversi e aggiornato il workflow Python per includere il branch corrente.

### Stato delle prove
- Il controllo GitHub effettuato dopo il commit non ha restituito status check né run associato all'head candidato. La nuova suite non è quindi certificata come eseguita.
- Il run precedente #40121561641 è riferito a job "Customer bot pass" e non prova, da solo, che la nuova suite MultiVocale sia stata eseguita sul nuovo head.
- Nessun test locale, E2E Meta, deploy Render, modifica di configurazione o scrittura database eseguiti in questo passaggio.

### Gate prima di un deploy staging
1. Ottenere un run Python verde sul commit esatto.
2. Rivedere diff e verificare la copertura dei test per tutte le chiavi namespaced.
3. La staging Render è ancora sul vecchio branch multivocale-runtime-v2; la produzione è ancora su main. Non sono state distribuite queste modifiche.
4. Prima di sostituire il runtime staging, drenare o migrare in modo esplicito batch/cleanup presenti nel vecchio namespace vf:mv:v2. Il cambio di prefisso renderebbe altrimenti inaccessibili al nuovo coordinator gli eventuali batch pendenti e la vecchia cleanup queue.
5. Non dichiarare risolti i rischi di persistenza: il Redis condiviso ha persistenza su disco disattivata; l'isolamento dei namespace non risolve la perdita di stato in caso di perdita completa di Redis.

Release gate invariato: nessun merge, deploy, invio WhatsApp reale, modifica a schema o dati live senza autorizzazione separata.


## Follow-up review — timing override corrected (2026-10-25)

A second static pass found that the transport-aware subclass in multivocale_runtime_v2_timing.py still wrote due times to the old global DUE_KEY, overriding the namespaced scheduling method in the base class. This would have left one queue shared despite the broader namespace change.

- Corrected the timing override to write through self._due_key(config).
- Updated timing unit tests to assert against the namespaced due key.
- Runtime fix commit: 9997affa1b4d1afe4f0eaa17a1345af071020284.
- Timing test commit: 3e80f7034205ad6271b411e9394ebabea258f740.
- Static reread confirms the timing override now uses the namespaced due key and the old global DUE_KEY write is absent.
- GitHub status checks are still empty and the connector returned no workflow run for the candidate commits. This is static confirmation only, not a test pass.

No deploy, merge, or live configuration change has been made.


## Follow-up operativo — verifica head e prossimi gate (2026-10-25)

### Nuove verifiche statiche
- Riletto il file `multivocale_runtime_v2_timing.py` sul branch candidato: l'override di `_schedule` usa ora `self._due_key(config)`; non scrive più direttamente sulla costante globale `DUE_KEY`.
- Riletto `.github/workflows/test-multivocale-runtime-v2.yml`: il workflow include sia `multivocale-runtime-v2` sia `assistant-conversation-photo-v1` tra i trigger push e definisce anche `workflow_dispatch`.
- Riletto il percorso di processing: la lease predefinita è 120 secondi ed esiste un thread di rinnovo ogni circa un terzo della lease; se il rinnovo fallisce, il thread termina dopo un log. Questo riduce il rischio di scadenza durante un'elaborazione lunga, ma il comportamento quando il rinnovo fallisce e il processing continua richiede un test mirato e non è dimostrato dalla lettura statica.

### Stato test aggiornato
- Per head `3e80f7034205ad6271b411e9394ebabea258f740`, GitHub ha restituito status checks vuoti e nessun workflow run associato tramite il connettore disponibile.
- È stato tentato anche un clone locale del repository per eseguire la suite, ma l'ambiente di esecuzione non riesce a risolvere `github.com`; quindi non è stato possibile eseguire la suite localmente in questo passaggio.
- Non dichiarare la suite superata. Il workflow è configurato, ma la sua esecuzione sul commit esatto resta da ottenere/verificare.

### Gate aggiuntivi prima di staging
1. Eseguire e verificare la suite Python sul commit esatto, inclusi i test del sottotipo timing.
2. Aggiungere/validare un test in cui il rinnovo lease fallisce mentre l'elaborazione supera la durata iniziale: il vecchio fencing token non deve poter inviare o finalizzare dopo che un altro worker ha acquisito il batch.
3. Verificare che il percorso audio attivo sia quello persistente previsto e documentare quali strutture legacy in memoria sono ancora raggiungibili; non dedurre dalla sola presenza delle variabili globali che siano o non siano usate dal percorso attivo.
4. Prima di qualsiasi cambio di branch staging, fare inventario/drain o migrazione verificata di batch e cleanup nel namespace precedente. Redis senza persistenza su disco resta un rischio separato.

Nessun test locale o E2E è stato completato in questo passaggio; nessun merge, deploy, modifica Render o scrittura live eseguiti.
