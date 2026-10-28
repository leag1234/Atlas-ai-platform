/* REQ-PROJ-08: UI locale only; content and server diagnostics retain their language. */
(() => {
  'use strict';
  const keys = 'projects organization project conversations newProject newSession name create cancel close ordinary chat work send stop message upload server environment context results ledger local onServer model produced instructions memory files coverage connectors imagePolicy approvals accept deny expires download preview jobs scheduled unavailable unknown empty loading failure fallback selected limits seconds cost sourceBytes payloadBytes uniqueBytes calls asOf repository device workspace status refresh session allSessions saved stopped failed completed running ready cancelled awaiting_approval possibly_sent acknowledged not_sent selectionError version validation format bytes approveRecorded shellFailure signIn selectedFiles'.split(' ');
  const rows = {
    en: ['Projects','Organization','Project','Conversations','New project','New session','Name','Create','Cancel','Close','Ordinary chat','Chat','Work','Send','Stop','Message','Upload to server','Server','Environment','Context','Results','Data ledger','Local','On server','Sent to model','Produced','Instructions version','Memory','Files','Coverage','Connectors','Image policy','Pending approvals','Accept','Deny','Expires','Download','Preview','Jobs','Scheduled tasks — unavailable until P1','Unavailable','Unknown','None','Loading…','Operation failed','Open project workspace','Selected','Limits','seconds','Cost EUR','Source bytes','Payload bytes','Unique source bytes','Calls','As of','Repository','Device unavailable','Workspace','Status','Refresh','Work session','All sessions','Saved','Stopped','Failed','Completed','Running','Ready','Cancelled','Awaiting approval','Possibly sent','Acknowledged','Not sent','Select an authorized project and conversation','Version','Validation','Format','Bytes','Decision recorded; execution is separate','Project shell could not be mounted','Sign in','Selected file versions'],
    fr: ['Projets','Organisation','Projet','Conversations','Nouveau projet','Nouvelle session','Nom','Créer','Annuler','Fermer','Discussion ordinaire','Discuter','Travailler','Envoyer','Arrêter','Message','Téléverser sur le serveur','Serveur','Environnement','Contexte','Résultats','Registre des données','Local','Sur le serveur','Envoyé au modèle','Produit','Version des instructions','Mémoire','Fichiers','Couverture','Connecteurs','Politique des images','Approbations en attente','Accepter','Refuser','Expiration','Télécharger','Aperçu','Tâches','Tâches planifiées — indisponibles avant P1','Indisponible','Inconnu','Aucun','Chargement…','Échec de l’opération','Ouvrir l’espace projet','Sélectionné','Limites','secondes','Coût EUR','Octets sources','Octets transmis','Octets sources uniques','Appels','À la date de','Dépôt','Appareil indisponible','Espace de travail','État','Actualiser','Session de travail','Toutes les sessions','Enregistré','Arrêté','Échec','Terminé','En cours','Prêt','Annulé','Approbation en attente','Peut-être envoyé','Confirmé','Non envoyé','Sélectionnez un projet et une conversation autorisés','Version','Validation','Format','Octets','Décision enregistrée ; exécution séparée','Impossible d’afficher le projet dans cette interface','Se connecter','Versions de fichiers sélectionnées'],
    de: ['Projekte','Organisation','Projekt','Unterhaltungen','Neues Projekt','Neue Sitzung','Name','Erstellen','Abbrechen','Schließen','Normaler Chat','Besprechen','Arbeiten','Senden','Stoppen','Nachricht','Auf Server hochladen','Server','Umgebung','Kontext','Ergebnisse','Datenprotokoll','Lokal','Auf dem Server','An das Modell gesendet','Erstellt','Anweisungsversion','Gedächtnis','Dateien','Abdeckung','Konnektoren','Bildrichtlinie','Ausstehende Genehmigungen','Erlauben','Ablehnen','Ablauf','Herunterladen','Vorschau','Aufträge','Geplante Aufgaben — erst ab P1 verfügbar','Nicht verfügbar','Unbekannt','Keine','Wird geladen…','Vorgang fehlgeschlagen','Projektbereich öffnen','Ausgewählt','Grenzen','Sekunden','Kosten EUR','Quellbytes','Übertragene Bytes','Eindeutige Quellbytes','Aufrufe','Stand','Repository','Gerät nicht verfügbar','Arbeitsbereich','Status','Aktualisieren','Arbeitssitzung','Alle Sitzungen','Gespeichert','Gestoppt','Fehlgeschlagen','Abgeschlossen','Läuft','Bereit','Abgebrochen','Wartet auf Genehmigung','Möglicherweise gesendet','Bestätigt','Nicht gesendet','Berechtigtes Projekt und Unterhaltung auswählen','Version','Validierung','Format','Bytes','Entscheidung gespeichert; Ausführung folgt separat','Projektoberfläche konnte nicht eingebunden werden','Anmelden','Ausgewählte Dateiversionen'],
    es: ['Proyectos','Organización','Proyecto','Conversaciones','Nuevo proyecto','Nueva sesión','Nombre','Crear','Cancelar','Cerrar','Chat normal','Conversar','Trabajar','Enviar','Detener','Mensaje','Subir al servidor','Servidor','Entorno','Contexto','Resultados','Registro de datos','Local','En el servidor','Enviado al modelo','Producido','Versión de instrucciones','Memoria','Archivos','Cobertura','Conectores','Política de imágenes','Aprobaciones pendientes','Aceptar','Denegar','Caduca','Descargar','Vista previa','Trabajos','Tareas programadas — no disponibles hasta P1','No disponible','Desconocido','Ninguno','Cargando…','La operación falló','Abrir espacio del proyecto','Seleccionado','Límites','segundos','Coste EUR','Bytes de origen','Bytes transmitidos','Bytes de origen únicos','Peticiones','A fecha de','Repositorio','Dispositivo no disponible','Espacio de trabajo','Estado','Actualizar','Sesión de trabajo','Todas las sesiones','Guardado','Detenido','Fallido','Completado','En curso','Listo','Cancelado','Esperando aprobación','Posiblemente enviado','Confirmado','No enviado','Seleccione un proyecto y una conversación autorizados','Versión','Validación','Formato','Bytes','Decisión registrada; ejecución por separado','No se pudo integrar la interfaz del proyecto','Iniciar sesión','Versiones de archivos seleccionadas'],
    it: ['Progetti','Organizzazione','Progetto','Conversazioni','Nuovo progetto','Nuova sessione','Nome','Crea','Annulla','Chiudi','Chat normale','Discuti','Lavora','Invia','Ferma','Messaggio','Carica sul server','Server','Ambiente','Contesto','Risultati','Registro dei dati','Locale','Sul server','Inviato al modello','Prodotto','Versione istruzioni','Memoria','File','Copertura','Connettori','Politica immagini','Approvazioni in attesa','Accetta','Rifiuta','Scadenza','Scarica','Anteprima','Lavori','Attività pianificate — non disponibili fino a P1','Non disponibile','Sconosciuto','Nessuno','Caricamento…','Operazione fallita','Apri spazio del progetto','Selezionato','Limiti','secondi','Costo EUR','Byte sorgente','Byte trasmessi','Byte sorgente unici','Chiamate','Aggiornato al','Repository','Dispositivo non disponibile','Spazio di lavoro','Stato','Aggiorna','Sessione di lavoro','Tutte le sessioni','Salvato','Fermato','Fallito','Completato','In corso','Pronto','Annullato','In attesa di approvazione','Possibilmente inviato','Confermato','Non inviato','Seleziona un progetto e una conversazione autorizzati','Versione','Validazione','Formato','Byte','Decisione registrata; esecuzione separata','Impossibile integrare l’interfaccia del progetto','Accedi','Versioni di file selezionate']
  };
  keys.push('previewUnavailable');
  const previewMessages = {en:'Preview unavailable; download the original file.',fr:'Aperçu indisponible ; téléchargez le fichier original.',de:'Vorschau nicht verfügbar; Originaldatei herunterladen.',es:'Vista previa no disponible; descargue el archivo original.',it:'Anteprima non disponibile; scarica il file originale.'};
  for (const language of Object.keys(rows)) rows[language].push(previewMessages[language]);
  const stateKeys = 'allow ask pending approved denied expired active proposed excluded partial passed created regenerated edited_in_place delete overwrite queued succeeded cancelling'.split(' ');
  const states = {
    en: ['Allowed','Ask first','Pending','Approved','Denied','Expired','Active','Proposed','Excluded','Partial','Passed','Created','Regenerated','Edited in place','Delete','Overwrite','Queued','Succeeded','Cancelling'],
    fr: ['Autorisé','Demander','En attente','Approuvé','Refusé','Expiré','Actif','Proposé','Exclu','Partiel','Validé','Créé','Régénéré','Modifié sur place','Supprimer','Écraser','En file','Réussi','Annulation'],
    de: ['Erlaubt','Nachfragen','Ausstehend','Genehmigt','Abgelehnt','Abgelaufen','Aktiv','Vorgeschlagen','Ausgeschlossen','Teilweise','Geprüft','Erstellt','Neu erzeugt','Direkt bearbeitet','Löschen','Überschreiben','In Warteschlange','Erfolgreich','Wird abgebrochen'],
    es: ['Permitido','Preguntar','Pendiente','Aprobado','Denegado','Caducado','Activo','Propuesto','Excluido','Parcial','Validado','Creado','Regenerado','Editado en el original','Eliminar','Sobrescribir','En cola','Completado','Cancelando'],
    it: ['Consentito','Chiedi prima','In attesa','Approvato','Rifiutato','Scaduto','Attivo','Proposto','Escluso','Parziale','Verificato','Creato','Rigenerato','Modificato sul posto','Elimina','Sovrascrivi','In coda','Riuscito','Annullamento']
  };
  keys.push(...stateKeys);
  for (const language of Object.keys(rows)) rows[language].push(...states[language]);
  keys.push('extractBytes', 'imageBytes');
  const byteLabels = {en:['Extract bytes','Image bytes'],fr:['Octets extraits','Octets images'],de:['Extrahierte Bytes','Bildbytes'],es:['Bytes extraídos','Bytes de imágenes'],it:['Byte estratti','Byte immagini']};
  for (const language of Object.keys(rows)) rows[language].push(...byteLabels[language]);
  keys.push('unknownCost', 'reservedCost');
  const costLabels = {en:['Unknown usage reservation EUR','Active reservation EUR'],fr:['Réserve pour usage inconnu EUR','Réserve active EUR'],de:['Reservierung für unbekannte Nutzung EUR','Aktive Reservierung EUR'],es:['Reserva por uso desconocido EUR','Reserva activa EUR'],it:['Riserva per utilizzo sconosciuto EUR','Riserva attiva EUR']};
  for (const language of Object.keys(rows)) rows[language].push(...costLabels[language]);
  keys.push('details','characters','text','page','pages','paragraph','paragraphs','sheet','table','row','column','of','units','accessFailure','limitFailure','serviceFailure');
  const displayLabels = {
    en:['Details','characters','text','page','pages','paragraph','paragraphs','sheet','table','row','column','of','units','Access is unavailable. Sign in or select another project.','This request reached a limit.','The service is temporarily unavailable.'],
    fr:['Détails','caractères','texte','page','pages','paragraphe','paragraphes','feuille','tableau','ligne','colonne','sur','unités','Accès indisponible. Connectez-vous ou sélectionnez un autre projet.','Cette demande a atteint une limite.','Le service est temporairement indisponible.'],
    de:['Details','Zeichen','Text','Seite','Seiten','Absatz','Absätze','Blatt','Tabelle','Zeile','Spalte','von','Einheiten','Zugriff nicht verfügbar. Anmelden oder anderes Projekt wählen.','Diese Anfrage hat ein Limit erreicht.','Der Dienst ist vorübergehend nicht verfügbar.'],
    es:['Detalles','caracteres','texto','página','páginas','párrafo','párrafos','hoja','tabla','fila','columna','de','unidades','Acceso no disponible. Inicie sesión o seleccione otro proyecto.','Esta solicitud alcanzó un límite.','El servicio no está disponible temporalmente.'],
    it:['Dettagli','caratteri','testo','pagina','pagine','paragrafo','paragrafi','foglio','tabella','riga','colonna','di','unità','Accesso non disponibile. Accedi o seleziona un altro progetto.','Questa richiesta ha raggiunto un limite.','Il servizio è temporaneamente non disponibile.']
  };
  for (const language of Object.keys(rows)) rows[language].push(...displayLabels[language]);
  keys.push('contextUsed','contextTokens','contextEstimate','system','project_instructions','pinned_memory','tool_declarations','session_state','recent_turns','tool_results','document_passages');
  const contextLabels = {
    en: ['Context used','tokens','Estimated using','System','Project instructions','Pinned memory','Tool declarations','Session state','Recent turns','Tool results','Document passages'],
    fr: ['Contexte utilisé','tokens','Estimation','Système','Instructions du projet','Mémoire épinglée','Déclarations des outils','État de session','Tours récents','Résultats des outils','Passages documentaires'],
    de: ['Verwendeter Kontext','Tokens','Schätzmethode','System','Projektanweisungen','Angehefteter Speicher','Werkzeugdefinitionen','Sitzungszustand','Letzte Beiträge','Werkzeugergebnisse','Dokumentpassagen'],
    es: ['Contexto usado','tokens','Estimación','Sistema','Instrucciones del proyecto','Memoria fijada','Declaraciones de herramientas','Estado de sesión','Turnos recientes','Resultados de herramientas','Pasajes de documentos'],
    it: ['Contesto utilizzato','token','Stima','Sistema','Istruzioni del progetto','Memoria fissata','Dichiarazioni degli strumenti','Stato della sessione','Turni recenti','Risultati degli strumenti','Passaggi dei documenti']
  };
  for (const language of Object.keys(rows)) rows[language].push(...contextLabels[language]);
  keys.push('contextCleared');
  for (const language of Object.keys(rows)) rows[language].push({en:'Tool results cleared',fr:'Résultats des outils allégés',de:'Werkzeugergebnisse gekürzt',es:'Resultados de herramientas reducidos',it:'Risultati degli strumenti ridotti'}[language]);
  const projectLabels = {
  "yourProjects": [
    "Your projects",
    "Vos projets",
    "Ihre Projekte",
    "Tus proyectos",
    "I tuoi progetti"
  ],
  "sharedProjects": [
    "Shared with you",
    "Partagé avec vous",
    "Mit Ihnen geteilt",
    "Compartidos contigo",
    "Condivisi con te"
  ],
  "searchProjects": [
    "Search projects",
    "Rechercher un projet",
    "Projekte suchen",
    "Buscar proyectos",
    "Cerca progetti"
  ],
  "sortProjects": [
    "Sort projects",
    "Trier les projets",
    "Projekte sortieren",
    "Ordenar proyectos",
    "Ordina progetti"
  ],
  "recentActivity": [
    "Recent activity",
    "Activité récente",
    "Letzte Aktivität",
    "Actividad reciente",
    "Attività recente"
  ],
  "privateProject": [
    "Private project",
    "Projet privé",
    "Privates Projekt",
    "Proyecto privado",
    "Progetto privato"
  ],
  "noSharedProjects": [
    "No shared projects yet",
    "Aucun projet partagé pour le moment",
    "Noch keine geteilten Projekte",
    "Todavía no hay proyectos compartidos",
    "Nessun progetto condiviso per ora"
  ],
  "noProjects": [
    "Your projects start here",
    "Vos projets commencent ici",
    "Hier beginnen Ihre Projekte",
    "Tus proyectos empiezan aquí",
    "I tuoi progetti iniziano qui"
  ],
  "noSearchResults": [
    "Try another name.",
    "Essayez un autre nom.",
    "Versuchen Sie einen anderen Namen.",
    "Prueba con otro nombre.",
    "Prova un altro nome."
  ],
  "projectIntro": [
    "Bring your conversations and files together in a project.",
    "Réunissez vos conversations et vos fichiers dans un projet.",
    "Bündeln Sie Unterhaltungen und Dateien in einem Projekt.",
    "Reúne tus conversaciones y archivos en un proyecto.",
    "Riunisci conversazioni e file in un progetto."
  ],
  "renameProject": [
    "Rename project",
    "Renommer le projet",
    "Projekt umbenennen",
    "Renombrar proyecto",
    "Rinomina progetto"
  ],
  "noConversations": [
    "Start your first conversation above.",
    "Commencez votre première conversation ci-dessus.",
    "Beginnen Sie oben Ihre erste Unterhaltung.",
    "Inicia tu primera conversación arriba.",
    "Inizia la prima conversazione qui sopra."
  ],
  "composerPlaceholder": [
    "How can Atlas help with this project?",
    "Comment Atlas peut-il vous aider sur ce projet ?",
    "Wie kann Atlas bei diesem Projekt helfen?",
    "¿Cómo puede Atlas ayudarte con este proyecto?",
    "Come può Atlas aiutarti con questo progetto?"
  ],
  "instructionsPlaceholder": [
    "Add context and preferences for Atlas.",
    "Ajoutez le contexte et vos préférences pour Atlas.",
    "Kontext und Präferenzen für Atlas hinzufügen.",
    "Añade contexto y preferencias para Atlas.",
    "Aggiungi contesto e preferenze per Atlas."
  ],
  "dropFiles": [
    "Drop files here or choose files",
    "Déposez des fichiers ici ou sélectionnez-les",
    "Dateien hier ablegen oder auswählen",
    "Arrastra archivos aquí o selecciónalos",
    "Trascina i file qui o selezionali"
  ],
  "editInConversation": [
    "Request change",
    "Demander la modification",
    "Änderung anfordern",
    "Solicitar cambio",
    "Richiedi modifica"
  ],
  "save": [
    "Save",
    "Enregistrer",
    "Speichern",
    "Guardar",
    "Salva"
  ],
  "remove": [
    "Remove",
    "Retirer",
    "Entfernen",
    "Quitar",
    "Rimuovi"
  ],
  "confirmRemove": [
    "Remove this item?",
    "Retirer cet élément ?",
    "Diesen Eintrag entfernen?",
    "¿Quitar este elemento?",
    "Rimuovere questo elemento?"
  ]
};
  for (const [key, values] of Object.entries(projectLabels)) { keys.push(key); Object.keys(rows).forEach((language,index) => rows[language].push(values[index])); }
  for (const [key, values] of Object.entries({instructions:['Instructions','Instructions','Anweisungen','Instrucciones','Istruzioni'],upload:['Add files','Ajouter des fichiers','Dateien hinzufügen','Añadir archivos','Aggiungi file'],ordinary:['New chat','Nouvelle discussion','Neuer Chat','Nuevo chat','Nuova chat']})) Object.keys(rows).forEach((language,index) => rows[language][keys.indexOf(key)] = values[index]);
  const changeLabels = {"changeInstructions": ["Update the project instructions", "Modifier les instructions du projet", "Projektanweisungen ändern", "Modificar las instrucciones del proyecto", "Modifica le istruzioni del progetto"], "removeFile": ["Remove this project file", "Retirer ce fichier du projet", "Diese Projektdatei entfernen", "Quitar este archivo del proyecto", "Rimuovi questo file dal progetto"], "removeMemory": ["Delete this project memory entry", "Supprimer cette entrée de mémoire du projet", "Diesen Projektspeichereintrag löschen", "Eliminar esta entrada de memoria del proyecto", "Elimina questa voce di memoria del progetto"], "changeApproval": ["Request approval before changing the original.", "Demander mon approbation avant de modifier l’original.", "Vor Änderung des Originals meine Genehmigung einholen.", "Pedir mi aprobación antes de modificar el original.", "Chiedi la mia approvazione prima di modificare l’originale."]};
  for (const [key,values] of Object.entries(changeLabels)) {keys.push(key); Object.keys(rows).forEach((language,index) => rows[language].push(values[index]));}
  const adminLabels = {
  "Remote targets": [
    "Remote targets",
    "Cibles distantes",
    "Remote-Ziele",
    "Destinos remotos",
    "Destinazioni remote"
  ],
  "Remote execution targets": [
    "Remote execution targets",
    "Exécution à distance",
    "Remote-Ausführung",
    "Ejecución remota",
    "Esecuzione remota"
  ],
  "Register a dedicated Linux or macOS account. Host key fingerprints must be verified independently. Private keys stay in the runner broker.": [
    "Register a dedicated Linux or macOS account. Verify the host key independently. Private keys stay in the runner broker.",
    "Enregistrez un compte Linux ou macOS dédié. Vérifiez la clé de l’hôte séparément. Les clés privées restent dans le service d’exécution.",
    "Registrieren Sie ein dediziertes Linux- oder macOS-Konto. Prüfen Sie den Hostschlüssel unabhängig. Private Schlüssel bleiben im Ausführungsdienst.",
    "Registra una cuenta Linux o macOS dedicada. Verifica la clave del host por separado. Las claves privadas permanecen en el servicio de ejecución.",
    "Registra un account Linux o macOS dedicato. Verifica la chiave host separatamente. Le chiavi private restano nel servizio di esecuzione."
  ],
  "Probe": [
    "Check connection",
    "Tester la connexion",
    "Verbindung prüfen",
    "Probar conexión",
    "Verifica connessione"
  ],
  "Grant to this project": [
    "Grant to this project",
    "Autoriser pour ce projet",
    "Für dieses Projekt erlauben",
    "Autorizar para este proyecto",
    "Autorizza per questo progetto"
  ],
  "Revoke from this project": [
    "Revoke from this project",
    "Révoquer pour ce projet",
    "Für dieses Projekt widerrufen",
    "Revocar para este proyecto",
    "Revoca per questo progetto"
  ],
  "Command audit": [
    "Command history",
    "Historique des commandes",
    "Befehlsverlauf",
    "Historial de comandos",
    "Cronologia comandi"
  ],
  "Target granted to this project.": [
    "Target granted to this project.",
    "Cible autorisée pour ce projet.",
    "Ziel für dieses Projekt erlaubt.",
    "Destino autorizado para este proyecto.",
    "Destinazione autorizzata per questo progetto."
  ],
  "Grant and egress permission revoked.": [
    "Grant and transfer permission revoked.",
    "Autorisations d’accès et de transfert révoquées.",
    "Zugriffs- und Übertragungsrechte widerrufen.",
    "Permisos de acceso y transferencia revocados.",
    "Permessi di accesso e trasferimento revocati."
  ],
  "Name": [
    "Name",
    "Nom",
    "Name",
    "Nombre",
    "Nome"
  ],
  "Host": [
    "Host",
    "Hôte",
    "Host",
    "Host",
    "Host"
  ],
  "SSH port": [
    "SSH port",
    "Port SSH",
    "SSH-Port",
    "Puerto SSH",
    "Porta SSH"
  ],
  "SSH user": [
    "SSH user",
    "Utilisateur SSH",
    "SSH-Benutzer",
    "Usuario SSH",
    "Utente SSH"
  ],
  "Working root": [
    "Working root",
    "Répertoire de travail",
    "Arbeitsverzeichnis",
    "Directorio de trabajo",
    "Directory di lavoro"
  ],
  "Pinned host key SHA256 fingerprints (one per line)": [
    "Host key SHA256 fingerprints (one per line)",
    "Empreintes SHA256 des clés hôtes (une par ligne)",
    "SHA256-Hostschlüssel (einer pro Zeile)",
    "Huellas SHA256 de claves host (una por línea)",
    "Impronte SHA256 delle chiavi host (una per riga)"
  ],
  "Credential reference": [
    "Credential reference",
    "Référence des identifiants",
    "Zugangsdatenreferenz",
    "Referencia de credenciales",
    "Riferimento credenziali"
  ],
  "Credential public key fingerprint": [
    "Public key fingerprint",
    "Empreinte de clé publique",
    "Fingerabdruck des öffentlichen Schlüssels",
    "Huella de clave pública",
    "Impronta della chiave pubblica"
  ],
  "Allow command rules (one per line)": [
    "Allowed commands (one per line)",
    "Commandes autorisées (une par ligne)",
    "Erlaubte Befehle (einer pro Zeile)",
    "Comandos permitidos (uno por línea)",
    "Comandi consentiti (uno per riga)"
  ],
  "Deny command rules (one per line)": [
    "Denied commands (one per line)",
    "Commandes interdites (une par ligne)",
    "Verbotene Befehle (einer pro Zeile)",
    "Comandos prohibidos (uno por línea)",
    "Comandi vietati (uno per riga)"
  ],
  "Target operating system": [
    "Operating system",
    "Système d’exploitation",
    "Betriebssystem",
    "Sistema operativo",
    "Sistema operativo"
  ],
  "Permission mode": [
    "Permission mode",
    "Mode d’autorisation",
    "Berechtigungsmodus",
    "Modo de permisos",
    "Modalità autorizzazioni"
  ],
  "Generate a key pair": [
    "Generate a key pair",
    "Générer une paire de clés",
    "Schlüsselpaar erzeugen",
    "Generar un par de claves",
    "Genera una coppia di chiavi"
  ],
  "Register target": [
    "Register target",
    "Enregistrer la cible",
    "Ziel registrieren",
    "Registrar destino",
    "Registra destinazione"
  ],
  "Install this public key in the target account. It is shown once:": [
    "Install this public key in the target account. It is shown once:",
    "Installez cette clé publique sur le compte cible. Elle ne sera affichée qu’une fois :",
    "Installieren Sie diesen öffentlichen Schlüssel im Zielkonto. Er wird nur einmal angezeigt:",
    "Instala esta clave pública en la cuenta de destino. Solo se muestra una vez:",
    "Installa questa chiave pubblica nell’account di destinazione. Viene mostrata una sola volta:"
  ]
};
  for (const [key,values] of Object.entries(adminLabels)) {keys.push(key); Object.keys(rows).forEach((language,index) => rows[language].push(values[index]));}
  const approvalLabels = {
  "verification": [
    "Verification",
    "Vérification",
    "Prüfung",
    "Verificación",
    "Verifica"
  ],
  "exited": [
    "exited",
    "code de sortie",
    "Beendigungscode",
    "código de salida",
    "codice di uscita"
  ],
  "directory": [
    "Directory",
    "Répertoire",
    "Verzeichnis",
    "Directorio",
    "Directory"
  ],
  "target": [
    "Target",
    "Cible",
    "Ziel",
    "Destino",
    "Destinazione"
  ],
  "codeDigest": [
    "Code digest",
    "Empreinte du code",
    "Code-Prüfsumme",
    "Huella del código",
    "Impronta del codice"
  ],
  "timeout": [
    "timeout",
    "délai",
    "Zeitlimit",
    "tiempo límite",
    "tempo limite"
  ],
  "codeTransfer": [
    "This sends the selected workspace to this target. Approval permits code transfer for this project.",
    "L’espace de travail sélectionné sera envoyé à cette cible. L’approbation autorise le transfert du code pour ce projet.",
    "Der ausgewählte Arbeitsbereich wird an dieses Ziel gesendet. Die Genehmigung erlaubt den Codetransfer für dieses Projekt.",
    "El espacio seleccionado se enviará a este destino. La aprobación permite transferir código para este proyecto.",
    "Lo spazio selezionato verrà inviato a questa destinazione. L’approvazione consente il trasferimento del codice per questo progetto."
  ],
  "alwaysAllow": [
    "Always allow commands starting with this command and its arguments (project rule)",
    "Toujours autoriser les commandes commençant par cette commande et ses arguments (règle du projet)",
    "Befehle mit diesem Befehl und diesen Argumenten immer erlauben (Projektregel)",
    "Permitir siempre comandos que empiecen por este comando y sus argumentos (regla del proyecto)",
    "Consenti sempre i comandi che iniziano con questo comando e i suoi argomenti (regola del progetto)"
  ],
  "Branch": [
    "Branch",
    "Branche",
    "Branch",
    "Rama",
    "Ramo"
  ],
  "Target": [
    "Target",
    "Cible",
    "Ziel",
    "Destino",
    "Destinazione"
  ],
  "Commit message": [
    "Commit message",
    "Message de commit",
    "Commit-Nachricht",
    "Mensaje de commit",
    "Messaggio di commit"
  ],
  "PR title": [
    "PR title",
    "Titre de la PR",
    "PR-Titel",
    "Título de la PR",
    "Titolo della PR"
  ],
  "PR body": [
    "PR description",
    "Description de la PR",
    "PR-Beschreibung",
    "Descripción de la PR",
    "Descrizione della PR"
  ],
  "Base commit": [
    "Base commit",
    "Commit de base",
    "Basis-Commit",
    "Commit base",
    "Commit di base"
  ],
  "rules": [
    "Rules",
    "Règles",
    "Regeln",
    "Reglas",
    "Regole"
  ],
  "auto": [
    "Automatic",
    "Automatique",
    "Automatisch",
    "Automático",
    "Automatico"
  ]
};
  for (const [key,values] of Object.entries(approvalLabels)) {keys.push(key); Object.keys(rows).forEach((language,index) => rows[language].push(values[index]));}
  const resultLabels = {"steps": ["Steps", "Étapes", "Schritte", "Pasos", "Passaggi"],"not_run": ["Not run", "Non exécuté", "Nicht ausgeführt", "Sin ejecutar", "Non eseguito"], "added": ["Added", "Ajouté", "Hinzugefügt", "Añadido", "Aggiunto"], "modified": ["Modified", "Modifié", "Geändert", "Modificado", "Modificato"], "deleted": ["Deleted", "Supprimé", "Gelöscht", "Eliminado", "Eliminato"]};
  for (const [key,values] of Object.entries(resultLabels)) {keys.push(key); Object.keys(rows).forEach((language,index) => rows[language].push(values[index]));}
  window.atlasProjectLocale = { keys, rows, language() {
    return (localStorage.getItem('locale') || localStorage.getItem('atlas-ui-language') || navigator.language || 'fr').replaceAll('"', '').split('-')[0];
  }, text(key) {
    const index = keys.indexOf(key), row = rows[this.language()] || rows.en;
    return index < 0 ? key : row[index];
  }};
})();
