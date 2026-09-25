outlook-grid-price-rise =
    { $variant ->
       [0] Elprisen stiger { $period_text }.
       [1] Elprisen ligger højere { $period_text }.
       [2] Der gælder en højere elpris { $period_text }.
      *[other] Prognosen viser, at elprisen stiger.
    }
outlook-grid-price-fall =
    { $variant ->
       [0] Elprisen falder { $period_text }.
       [1] Elprisen ligger lavere { $period_text }.
       [2] Der gælder en lavere elpris { $period_text }.
      *[other] Prognosen viser, at elprisen falder.
    }
outlook-limited-grid-use =
    { $variant ->
       [0] Der forventes kun et lille elforbrug fra elnettet { $period_text }.
       [1] Elforbruget fra elnettet forventes at være lavt { $period_text }.
       [2] Kun en lille mængde strøm fra elnettet er forventet.
      *[other] Der forventes lavt elforbrug fra elnettet.
    }
outlook-flat-grid-prices =
    { $variant ->
       [0] Elprisen holder sig stabil { $period_text }.
       [1] Prognosen viser kun små udsving i elprisen { $period_text }.
       [2] Der er kun små ændringer i elprisen { $period_text }.
      *[3] Elprisen forbliver forholdsvis stabil { $period_text }.
    }
outlook-negative-grid-price =
    { $variant ->
       [0] Elprisen er under nul fra kl. { $start } til kl. { $end }.
       [1] Der er en negativ elpris fra kl. { $start } til kl. { $end }.
       [2] Elprisen falder under nul fra kl. { $start } til kl. { $end }.
      *[other] Prognosen viser en negativ elpris fra kl. { $start } til kl. { $end }.
    }
outlook-cheaper-grid-prices =
    { $variant ->
       [0] Der er en lavere elpris fra kl. { $start } til kl. { $end }.
       [1] Elprisen dykker fra kl. { $start } til kl. { $end }.
       [2] Elprisen ligger lavere fra kl. { $start } til kl. { $end }.
      *[other] Prognosen viser en billigere elpris fra kl. { $start } til kl. { $end }.
    }
outlook-grid-price-swing =
    { $variant ->
       [0] Elprisen skifter retning omkring kl. { $turn_at }.
       [1] Prognosen for elprisen vender omkring kl. { $turn_at }.
       [2] Et skift i elprisen sker omkring kl. { $turn_at }.
      *[other] Elprisen bevæger sig den modsatte vej efter kl. { $turn_at }.
    }
outlook-solar-surplus =
    { $variant ->
       [0] Solproduktionen forventes at overstige det forventede forbrug fra kl. { $start } til kl. { $end } og toppe omkring kl. { $peak_at }.
       [1] Der forventes mere solstrøm end forbrug fra kl. { $start } til kl. { $end }, med en top omkring kl. { $peak_at }.
       [2] Solproduktionen bør være højere end det forventede forbrug fra kl. { $start } til kl. { $end } og toppe omkring kl. { $peak_at }.
      *[other] Solen forventes at dække mere end belastningen fra kl. { $start } til kl. { $end }.
    }
outlook-solar-modest =
    { $variant ->
       [0] Solproduktionen stiger langsomt frem mod kl. { $peak_at } og forbliver under det forventede forbrug.
       [1] Der forventes en mindre soltop omkring kl. { $peak_at }, under det forventede forbrug.
       [2] Solproduktionen vokser lidt frem mod kl. { $peak_at }, men dækker ikke det forventede forbrug.
      *[other] Solproduktionen forventes fortsat at være lavere end forbruget omkring kl. { $peak_at }.
    }
outlook-solar-fading =
    { $variant ->
       [0] Solproduktionen forventes at falde efter cirka kl. { $start }.
       [1] Solproduktionen begynder at falde omkring kl. { $start }.
       [2] Der forventes mindre solstrøm efter cirka kl. { $start }.
      *[other] Solproduktionen aftager efter kl. { $start }.
    }
outlook-low-reserve =
    { $variant ->
       [0] Den aktuelle opladning i { $subject } er tæt på minimum.
       [1] { $subject } har kun lidt opladning over minimumsniveauet lige nu.
       [2] Batteriniveauet i { $subject } er tæt på minimum.
      *[other] { $subject } har begrænsede reserver over minimum.
    }
outlook-low-reserve-solo =
    { $variant ->
       [0] Opladningen er tæt på minimum.
       [1] Der er kun lidt opladning over minimumsniveauet lige nu.
       [2] Reserverne er tæt på batteriets minimum.
      *[other] Der er kun begrænsede reserver over minimum.
    }
outlook-grid-charge =
    { $variant ->
       [0] { $subject } er planlagt til opladning fra elnettet fra kl. { $start } til kl. { $end }.
       [1] { $subject } oplades fra elnettet fra kl. { $start } til kl. { $end }.
       [2] Opladning fra elnettet er planlagt for { $subject } fra kl. { $start } til kl. { $end }.
      *[other] { $subject } forventes at bruge strøm fra elnettet til opladning fra kl. { $start } til kl. { $end }.
    }
outlook-grid-charge-solo =
    { $variant ->
       [0] Opladning fra elnettet kører fra kl. { $start } til kl. { $end }.
       [1] Opladning fra elnettet er planlagt fra kl. { $start } til kl. { $end }.
       [2] Batteriet oplades fra elnettet fra kl. { $start } til kl. { $end }.
      *[other] Strøm fra elnettet bruges til opladning fra kl. { $start } til kl. { $end }.
    }
outlook-battery-preserve =
    { $variant ->
       [0] { $subject } er planlagt til at bevare opladningen fra kl. { $start } til kl. { $end }.
       [1] Energien i { $subject } gemmes fra kl. { $start } til kl. { $end }.
       [2] { $subject } er planlagt til ikke at aflade fra kl. { $start } til kl. { $end }.
      *[other] Opladningen i { $subject } bevares fra kl. { $start } til kl. { $end }.
    }
outlook-battery-preserve-solo =
    { $variant ->
       [0] Den gemte energi bevares fra kl. { $start } til kl. { $end }.
       [1] Opladningen holdes fra kl. { $start } til kl. { $end }.
       [2] Afladning er sat på pause fra kl. { $start } til kl. { $end }.
      *[other] Den gemte opladning bevares fra kl. { $start } til kl. { $end }.
    }
outlook-battery-self-consume =
    { $variant ->
       [0] { $subject } har ingen planlagt opladning fra elnettet eller bevaret opladning.
       [1] Ingen netopladning eller bevaret opladning for { $subject }.
       [2] { $subject } forbliver i normal egenforbrugstilstand.
      *[other] { $subject } er planlagt til normalt eget forbrug.
    }
outlook-battery-self-consume-solo =
    { $variant ->
       [0] Ingen opladning fra elnettet eller bevaret opladning er planlagt.
       [1] Normalt eget forbrug fortsætter.
       [2] Batteriet forbliver i egenforbrugstilstand.
      *[other] Eget forbrug er planlagt.
    }
outlook-battery-full =
    { $variant ->
       [0] { $subject } forventes at være fuldt opladet senest kl. { $start }.
       [1] { $subject } bør nå fuld opladning senest kl. { $start }.
       [2] Prognosen viser { $subject } fuldt opladet senest kl. { $start }.
      *[other] { $subject } forventes fyldt op senest kl. { $start }.
    }
outlook-battery-full-solo =
    { $variant ->
       [0] Batteriet forventes at være fuldt opladet senest kl. { $start }.
       [1] Fuld opladning bør nås senest kl. { $start }.
       [2] Prognosen viser et fuldt batteri senest kl. { $start }.
      *[other] Et fuldt batteri forventes senest kl. { $start }.
    }
outlook-target-shortfall =
    { $variant ->
       [0] { $subject } forventes kun at nå { $expected } % senest kl. { $start }; målet er { $requested } %.
       [1] Kl. { $start } forventes { $subject } at være på { $expected } % i stedet for målet på { $requested } %.
       [2] { $subject } når muligvis kun { $expected } % kl. { $start }, under målet på { $requested } %.
      *[other] Prognosen for { $subject } er { $expected } % kl. { $start }, under målet på { $requested } %.
    }
outlook-target-shortfall-known-solo =
    { $variant ->
       [0] Der forventes kun { $expected } % senest kl. { $start }; målet er { $requested } %.
       [1] Kl. { $start } forventes batteriet at være på { $expected } % i stedet for målet på { $requested } %.
       [2] Batteriet når muligvis kun { $expected } % kl. { $start }, under målet på { $requested } %.
      *[other] Prognosen er { $expected } % kl. { $start }, under målet på { $requested } %.
    }
outlook-target-shortfall-missing-solo =
    { $variant ->
       [0] Målet forventes ikke nået senest kl. { $start }.
       [1] Prognosen efterlader batteriet under målet kl. { $start }.
       [2] Det ønskede opladningsniveau nås muligvis ikke senest kl. { $start }.
      *[other] Batteriet når muligvis ikke målet senest kl. { $start }.
    }
outlook-target-reached =
    { $variant ->
       [0] { $subject } forventes at nå målet på { $requested } % senest kl. { $start }.
       [1] { $subject } bør oplades til { $requested } % senest kl. { $start }.
       [2] { $subject } forventes at være på { $requested } % kl. { $start }.
      *[other] Målet på { $requested } % for { $subject } forventes nået senest kl. { $start }.
    }
outlook-target-reached-known-solo =
    { $variant ->
       [0] Målet på { $requested } % forventes nået senest kl. { $start }.
       [1] Opladning til { $requested } % bør være færdig senest kl. { $start }.
       [2] Prognosen har batteriet på { $requested } % kl. { $start }.
      *[other] Batteriet bør nå { $requested } % senest kl. { $start }.
    }
outlook-target-reached-missing-solo =
    { $variant ->
       [0] Målet forventes nået senest kl. { $start }.
       [1] Det ønskede opladningsniveau bør nås senest kl. { $start }.
       [2] Batteriet bør være på målet senest kl. { $start }.
      *[other] Målet bør nås senest kl. { $start }.
    }
outlook-comfort-timing =
    { $variant ->
       [0] { $subject } er planlagt fra kl. { $start } til kl. { $end }, mens der forventes solproduktion.
       [1] { $subject } kører fra kl. { $start } til kl. { $end }, mens solen forventes at producere strøm.
       [2] { $subject } er planlagt fra kl. { $start } til kl. { $end } for at overlappe den forventede solproduktion.
      *[other] Forventet solproduktion overlapper { $subject } fra kl. { $start } til kl. { $end }.
    }
outlook-optional-start =
    { $variant ->
       [0] Det bedste tidspunkt at starte { $subject } is { $start }.
       [1] Start { $subject } at { $start }.
       [2] { $subject } anbefales kl. { $start }.
      *[other] Det foretrukne starttidspunkt for { $subject } is { $start }.
    }
outlook-grid-export =
    { $variant ->
       [0] Overskydende strøm forventes sendt til elnettet fra kl. { $start } til kl. { $end }.
       [1] Prognosen viser eksport af strøm til elnettet fra kl. { $start } til kl. { $end }.
       [2] Overskydende strøm sendes til elnettet fra kl. { $start } til kl. { $end }.
      *[other] Eksport til elnettet forventes fra kl. { $start } til kl. { $end }.
    }
outlook-heavy-grid-use =
    { $variant ->
       [0] Der forventes stort elforbrug fra nettet { $period_text }.
       [1] Forbruget af strøm fra elnettet forventes at være højt { $period_text }.
       [2] Det forventes, at en stor del af strømmen kommer fra elnettet.
      *[other] Boligen forventes at hente meget strøm fra elnettet.
    }
outlook-charging-dominates-imports =
    { $variant ->
       [0] Det meste af elforbruget fra nettet forventes at gå til batteriopladning.
       [1] Batteriopladning forventes at stå for det meste af forbruget fra elnettet.
       [2] Størstedelen af den importerede strøm forventes brugt til at oplade batterier.
      *[other] Elforbruget fra nettet er koncentreret om batteriopladning.
    }
outlook-grid-use-increase =
    { $variant ->
       [0] Elforbruget fra nettet forventes at stige senere.
       [1] Boligen forventes at hente mere strøm fra elnettet senere.
       [2] Forbruget fra elnettet bør være højere { $period_text }.
      *[other] Der forventes mere elforbrug fra nettet senere.
    }
outlook-grid-use-decrease =
    { $variant ->
       [0] Elforbruget fra nettet forventes at falde senere.
       [1] Boligen forventes at hente mindre strøm fra elnettet senere.
       [2] Forbruget fra elnettet bør være lavere { $period_text }.
      *[other] Der forventes mindre elforbrug fra nettet senere.
    }
outlook-source-problem =
    { $variant ->
       [0] Soldata er ikke blevet opdateret i { $elapsed_value } minut.
       [1] Opdateringer af soldata er forsinket i { $elapsed_value } minut.
       [2] Der bruges stadig ældre soldata efter { $elapsed_value } minut.
      *[other] Aktuelle soldata har været utilgængelige i { $elapsed_value } minut.
    }
outlook-source-problem-import-price =
    { $variant ->
       [0] Data om elprisen er ikke blevet opdateret i { $elapsed_value } minut.
       [1] Opdateringer af elprisen er forsinket i { $elapsed_value } minut.
       [2] Ældre data om elprisen bruges stadig efter { $elapsed_value } minut.
      *[other] Aktuelle data om elprisen har været utilgængelige i { $elapsed_value } minut.
    }
outlook-plan-refresh-failure =
    { $variant ->
    [0] Der har ikke været en ny plan i { $duration }. Den tidligere plan bruges stadig.
    [1] Planen er ikke blevet opdateret i { $duration }. WattPlan fortsætter med den tidligere plan.
    [2] En opdateret plan har manglet i { $duration }. Den seneste brugbare plan er stadig aktiv.
   *[3] WattPlan har brugt den samme plan i { $duration }, fordi en ny plan ikke er klar.
    }
outlook-plan-unavailable =
    { $variant ->
       [0] Planlægning er ikke tilgængelig lige nu. Der er ingen plan, der kan bruges.
       [1] WattPlan kan ikke lave en plan lige nu, så der er ingen tidsplan.
       [2] Der er i øjeblikket ingen plan, som WattPlan kan bruge.
      *[other] Der er ingen aktuel plan tilgængelig.
    }
outlook-plan-unusable =
    { $variant ->
       [0] Planlægningen er afbrudt. Den gemte plan kan ikke bruges nu.
       [1] WattPlan er sat på pause, fordi den gemte plan ikke kan bruges.
       [2] Den gemte plan kan ikke bruges lige nu, så planlægningen er sat på pause.
      *[other] Den gemte plan kan ikke bruges i øjeblikket.
    }
outlook-plan-expired =
    { $variant ->
       [0] Planlægningen er afbrudt, fordi den tidligere plan er udløbet.
       [1] Den tidligere plan er udløbet, så WattPlan venter på en ny.
       [2] WattPlan kan ikke fortsætte med den gamle plan, fordi den er udløbet.
      *[other] Planen er udløbet og afventer en ny.
    }
outlook-restored-unvalidated =
    { $variant ->
       [0] En ny plan er ikke klar efter genstart.
       [1] WattPlan er genstartet og venter på en ny plan, der kan bruges.
       [2] Planråd er sat på pause, indtil en ny plan er klar efter genstart.
      *[other] Anbefalinger afventer en ny plan efter genstart.
    }
outlook-recommendations-unavailable =
    { $variant ->
       [0] Der er ingen aktuelle anbefalinger til opladning eller planlagte enheder.
       [1] Aktuelle forslag til opladning og enheder er ikke tilgængelige.
       [2] WattPlan har ingen aktuelle anbefalinger til opladning eller enheder.
      *[other] Aktuelle anbefalinger er ikke tilgængelige.
    }
outlook-stored-recommendations-unvalidated =
    { $variant ->
       [0] Gemte forslag til opladning og enheder er endnu ikke aktuelle.
       [1] De gemte forslag venter på en ny plan, før de kan bruges.
       [2] De gemte råd om opladning og enheder er endnu ikke bekræftet af en ny plan.
      *[other] Gemte anbefalinger afventer validering.
    }
outlook-quiet =
    { $variant ->
       [0] Der forventes ingen vigtige ændringer i planen { $period_text }.
       [1] Planen forventes at være stort set uændret { $period_text }.
       [2] Der forventes ikke større ændringer i resten af planen.
      *[other] Den resterende plan forventes at være stabil.
    }
outlook-unknown = Planopdatering: { $kind }.

outlook-duration = { $unit ->
    [hour] { $value ->
        [1] en time
       *[other] { $value } timer
    }
   *[minute] { $value ->
        [1] et minut
       *[other] { $value } minutter
    }
}

outlook-period = { $period ->
    [rest-of-today] resten af dagen
    [today] i dag
    [tomorrow] i morgen
   *[weekday] på { $weekday ->
        [0] mandag
        [1] tirsdag
        [2] onsdag
        [3] torsdag
        [4] fredag
        [5] lørdag
       *[6] søndag
    }
}

outlook-grid-price-swing-ease-then-rise = { $variant ->
    [0] Elpriserne falder, før de stiger igen omkring kl. { $turn_at }.
    [1] Prognosen falder og vender derefter opad omkring kl. { $turn_at }.
   *[2] En lavere elpris forventes før en stigning omkring kl. { $turn_at }.
}
outlook-grid-price-swing-rise-then-ease = { $variant ->
    [0] Elpriserne stiger, før de falder omkring kl. { $turn_at }.
    [1] Prognosen stiger og vender derefter nedad omkring kl. { $turn_at }.
   *[2] En højere elpris forventes før et fald omkring kl. { $turn_at }.
}
outlook-optional-start-single = { $variant ->
    [0] Det bedste tidspunkt at starte { $subject } er kl. { $start }.
    [1] Start { $subject } kl. { $start }.
   *[2] { $subject } anbefales kl. { $start }.
}
outlook-optional-start-alternative = { $variant ->
    [0] Start { $subject } kl. { $start }; kl. { $alternative_at } er alternativet.
    [1] Kl. { $start } er første valg for { $subject }, men kl. { $alternative_at } kan også bruges.
    [2] { $subject } startes bedst kl. { $start }; et alternativ er kl. { $alternative_at }.
   *[3] For { $subject } er kl. { $start } første valg og kl. { $alternative_at } andet valg.
}
outlook-target-shortfall-known = { $variant ->
    [0] { $subject } forventes kun at nå { $expected } % senest kl. { $start }; målet er { $requested } %.
    [1] Kl. { $start } forventes { $subject } at være på { $expected } % i stedet for målet på { $requested } %.
    [2] { $subject } når muligvis kun { $expected } % kl. { $start }, under målet på { $requested } %.
   *[3] Prognosen for { $subject } er { $expected } % kl. { $start }, under målet på { $requested } %.
}
outlook-target-shortfall-missing = { $variant ->
    [0] { $subject } forventes ikke at nå målet senest kl. { $start }.
    [1] Prognosen placerer { $subject } under målet kl. { $start }.
   *[2] { $subject } når muligvis ikke det ønskede opladningsniveau senest kl. { $start }.
}
outlook-target-reached-known = { $variant ->
    [0] { $subject } forventes at nå målet på { $requested } % senest kl. { $start }.
    [1] { $subject } bør oplades til { $requested } % senest kl. { $start }.
   *[2] { $subject } forventes at være på { $requested } % kl. { $start }.
}
outlook-target-reached-missing = { $variant ->
    [0] { $subject } forventes at nå målet senest kl. { $start }.
    [1] Det forventes, at { $subject } når det ønskede opladningsniveau senest kl. { $start }.
   *[2] { $subject } bør være på målet senest kl. { $start }.
}
outlook-source-problem-import-price-stale = { $variant ->
       [0] Data om elprisen er forældede efter { $duration }; de seneste værdier bruges stadig.
       [1] Opdateringer af elprisen er forsinket i { $duration }, så WattPlan bruger tidligere priser.
      *[2] Der bygges videre på ældre data om elprisen efter { $duration }.
}
outlook-source-problem-import-price-unavailable = { $variant ->
       [0] Data om elprisen har været utilgængelige i { $duration }; en aktuel prisprognose kan ikke stoles på.
       [1] Der er ikke kommet en brugbar opdatering af elprisen i { $duration }.
      *[2] Data om elprisen er utilgængelige efter { $duration }, så prisråd er begrænsede.
}
outlook-source-problem-export-price-stale = { $variant ->
    [0] Data om eksportpriser er forældede efter { $duration }; tidligere værdier bruges stadig.
    [1] Opdateringer af eksportpriser er forsinket i { $duration }.
   *[2] Der bygges videre på ældre data om eksportpriser efter { $duration }.
}
outlook-source-problem-export-price-unavailable = { $variant ->
    [0] Data om eksportpriser har været utilgængelige i { $duration }; værdien af eksport er usikker.
    [1] Der er ikke kommet en brugbar opdatering af eksportpriser i { $duration }.
   *[2] Data om eksportpriser er utilgængelige efter { $duration }.
}
outlook-source-problem-usage-stale = { $variant ->
    [0] Forbrugsdata er forældede efter { $duration }; den seneste prognose bruges stadig.
    [1] Opdateringer af forbrug er forsinket i { $duration }.
   *[2] Der bygges videre på ældre forbrugsdata efter { $duration }.
}
outlook-source-problem-usage-unavailable = { $variant ->
    [0] Forbrugsdata har været utilgængelige i { $duration }; forbrugsbaserede råd er begrænsede.
    [1] Der er ikke kommet en brugbar opdatering af forbrug i { $duration }.
   *[2] Forbrugsdata er utilgængelige efter { $duration }.
}
outlook-source-problem-pv-stale = { $variant ->
    [0] Soldata er forældede efter { $duration }; den seneste prognose bruges stadig.
    [1] Opdateringer af soldata er forsinket i { $duration }.
    [2] Der bygges videre på en ældre solprognose efter { $duration }.
   *[3] Der er ikke kommet nye soldata i { $duration }; den tidligere prognose bruges stadig.
}
outlook-source-problem-pv-unavailable = { $variant ->
    [0] Soldata har været utilgængelige i { $duration }; solbaserede råd er begrænsede.
    [1] Der er ikke kommet en brugbar opdatering af soldata i { $duration }.
   *[2] Soldata er utilgængelige efter { $duration }.
}
