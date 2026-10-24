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
