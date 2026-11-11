-- Activation du sous-ensemble validé (mappé sans ambiguïté + comptes-rôles).
-- Le libellé FR curé et l'indice d'usage métier sont posés sur le compte Odoo correspondant.
-- Les comptes 'à confirmer' restent pending : le comptable les activera via la feuille
-- plan-comptable-mapping.csv (CLI `accounts import-mapping` ou activation manuelle).
-- Fichier généré — ne pas éditer à la main.

update accounts set label='Réserve légale', usage_hint='dotation à la réserve légale', status='active', validated_at=now() where code='111000';
update accounts set label='Report à nouveau créditeur', usage_hint='bénéfices reportés', status='active', validated_at=now() where code='121000';
update accounts set label='Résultat net de l''exercice : bénéfice', usage_hint='clôture annuelle', status='active', validated_at=now() where code='131000';
update accounts set label='Résultat net de l''exercice : perte', usage_hint='clôture annuelle', status='active', validated_at=now() where code='139000';
update accounts set label='Emprunts et dettes auprès des établissements de crédit', usage_hint='prêt bancaire, crédit moyen terme, remboursement du capital', status='active', validated_at=now() where code='162000';
update accounts set label='Matériel informatique', usage_hint='ordinateurs, imprimantes, serveurs, téléphones (au-dessus du seuil d''immobilisation)', status='active', validated_at=now() where code='244200';
update accounts set label='Matières consommables', usage_hint='stock de consommables', status='active', validated_at=now() where code='331000';
update accounts set label='Fournitures d''atelier et d''usine', usage_hint='stock de fournitures d''atelier', status='active', validated_at=now() where code='332000';
update accounts set label='Fournisseurs, dettes en compte', usage_hint='toute facture fournisseur à payer (contrepartie des achats)', status='active', validated_at=now() where code='401100';  -- 401 -> (compte-rôle, défaut à confirmer)
update accounts set label='Clients, factures à établir', usage_hint='livraison faite, facture pas encore émise', status='active', validated_at=now() where code='418100';
update accounts set label='Personnel, avances et acomptes', usage_hint='avance sur salaire, acompte, prêt au personnel', status='active', validated_at=now() where code='421100';  -- 421 -> (compte-rôle, défaut à confirmer)
update accounts set label='Personnel, rémunérations dues', usage_hint='salaires nets à payer', status='active', validated_at=now() where code='422000';
update accounts set label='État, impôt sur les bénéfices', usage_hint='impôt sur les sociétés dû, acomptes', status='active', validated_at=now() where code='441000';
update accounts set label='État, TVA facturée sur ventes', usage_hint='TVA collectée sur les ventes', status='active', validated_at=now() where code='443100';
update accounts set label='État, TVA due', usage_hint='TVA à reverser à l''État', status='active', validated_at=now() where code='444100';
update accounts set label='État, crédit de TVA à reporter', usage_hint='crédit de TVA', status='active', validated_at=now() where code='444900';
update accounts set label='État, TVA récupérable sur immobilisations', usage_hint='TVA sur achat d''immobilisations', status='active', validated_at=now() where code='445100';
update accounts set label='État, TVA récupérable sur achats', usage_hint='TVA déductible sur achats de biens et marchandises', status='active', validated_at=now() where code='445200';
update accounts set label='État, TVA récupérable sur services extérieurs et autres charges', usage_hint='TVA déductible sur loyers, honoraires, télécoms, entretien', status='active', validated_at=now() where code='445400';
update accounts set label='État, autres taxes sur chiffre d''affaires', usage_hint='taxes spécifiques sur le chiffre d''affaires', status='active', validated_at=now() where code='446000';
update accounts set label='Débiteurs divers', usage_hint='créance diverse non classée ailleurs', status='active', validated_at=now() where code='471100';
update accounts set label='Créditeurs divers', usage_hint='dette diverse non classée ailleurs', status='active', validated_at=now() where code='471200';
update accounts set label='Charges constatées d''avance', usage_hint='charge payée d''avance (assurance annuelle, loyer d''avance)', status='active', validated_at=now() where code='476000';
update accounts set label='Produits constatés d''avance', usage_hint='produit encaissé d''avance', status='active', validated_at=now() where code='477000';
update accounts set label='Banques locales', usage_hint='compte bancaire de la société (un sous-compte par banque : 5211, 5212…)', status='active', validated_at=now() where code='521001';  -- 521 -> (compte-rôle, défaut à confirmer)
update accounts set label='Crédits de trésorerie', usage_hint='découvert bancaire, avance à court terme', status='active', validated_at=now() where code='561000';
update accounts set label='Caisse siège social', usage_hint='espèces en caisse, petites dépenses, décaissements en cash', status='active', validated_at=now() where code='571100';  -- 571 -> (compte-rôle, défaut à confirmer)
update accounts set label='Régies d''avances', usage_hint='avance de fonds remise à un agent pour dépenses (régisseur)', status='active', validated_at=now() where code='581000';
update accounts set label='Fournitures non stockables, eau', usage_hint='facture d''eau (SNEC, SODECI…)', status='active', validated_at=now() where code='605100';
update accounts set label='Fournitures non stockables, électricité', usage_hint='facture d''électricité (ENEO, CIE…)', status='active', validated_at=now() where code='605200';
update accounts set label='Fournitures non stockables, autres énergies', usage_hint='carburant, gasoil, essence, gaz', status='active', validated_at=now() where code='605300';
update accounts set label='Fournitures d''entretien non stockables', usage_hint='produits d''entretien, nettoyage', status='active', validated_at=now() where code='605400';
update accounts set label='Fournitures de bureau non stockables', usage_hint='papeterie, cartouches, petites fournitures de bureau', status='active', validated_at=now() where code='605500';
update accounts set label='Achats de petit matériel et outillage', usage_hint='petit équipement, outils, matériel en dessous du seuil d''immobilisation', status='active', validated_at=now() where code='605600';
update accounts set label='Achats d''études et prestations de services', usage_hint='prestation de service achetée liée à une production', status='active', validated_at=now() where code='605700';
update accounts set label='Transports sur ventes', usage_hint='livraison aux clients', status='active', validated_at=now() where code='612000';
update accounts set label='Transports du personnel', usage_hint='transport du personnel, taxis, navettes', status='active', validated_at=now() where code='614000';
update accounts set label='Voyages et déplacements', usage_hint='billets d''avion, hôtel, déplacement professionnel', status='active', validated_at=now() where code='618100';
update accounts set label='Sous-traitance générale', usage_hint='travaux confiés à un sous-traitant', status='active', validated_at=now() where code='621000';
update accounts set label='Entretien et réparations des biens mobiliers', usage_hint='entretien et réparation des véhicules et du matériel', status='active', validated_at=now() where code='624200';
update accounts set label='Maintenance', usage_hint='contrats de maintenance (climatisation, ascenseur, informatique)', status='active', validated_at=now() where code='624300';
update accounts set label='Assurances matériel de transport', usage_hint='assurance des véhicules', status='active', validated_at=now() where code='625200';
update accounts set label='Frais de téléphone', usage_hint='forfaits téléphoniques, recharges, factures télécoms', status='active', validated_at=now() where code='628100';
update accounts set label='Autres frais de télécommunications', usage_hint='internet, data, abonnement fibre', status='active', validated_at=now() where code='628800';
update accounts set label='Frais de formation du personnel', usage_hint='formations, séminaires', status='active', validated_at=now() where code='633000';
update accounts set label='Réceptions', usage_hint='repas d''affaires, réceptions, cocktails', status='active', validated_at=now() where code='638300';
update accounts set label='Missions', usage_hint='frais de mission du personnel (per diem, hébergement)', status='active', validated_at=now() where code='638400';
update accounts set label='Patentes, licences et taxes annexes', usage_hint='patente, licence', status='active', validated_at=now() where code='641200';
update accounts set label='Impôts et taxes indirects', usage_hint='taxes indirectes', status='active', validated_at=now() where code='645000';
update accounts set label='Pertes de change sur créances et dettes commerciales', usage_hint='écart de change défavorable sur opérations commerciales', status='active', validated_at=now() where code='656000';
update accounts set label='Pertes de change financières', usage_hint='écart de change défavorable sur opérations financières', status='active', validated_at=now() where code='676000';
update accounts set label='Subventions d''exploitation', usage_hint='aides, subventions', status='active', validated_at=now() where code='711000';
update accounts set label='Gains de change sur créances et dettes commerciales', usage_hint='écart de change favorable sur opérations commerciales', status='active', validated_at=now() where code='756000';
update accounts set label='Gains de change financiers', usage_hint='écart de change favorable sur opérations financières', status='active', validated_at=now() where code='776000';
update accounts set label='Transferts de charges d''exploitation', usage_hint='remboursement de frais, transfert de charges', status='active', validated_at=now() where code='781000';
update accounts set label='Charges HAO constatées', usage_hint='charges exceptionnelles, hors activité ordinaire', status='active', validated_at=now() where code='831000';
update accounts set label='Produits HAO constatés', usage_hint='produits exceptionnels, hors activité ordinaire', status='active', validated_at=now() where code='841000';
update accounts set label='Impôt minimum forfaitaire', usage_hint='impôt minimum forfaitaire (IMF/minimum de perception)', status='active', validated_at=now() where code='895000';
