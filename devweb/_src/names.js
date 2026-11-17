// ============================================================================
// names - names/regions and names/get for every region: persona shape, ASCII
// folding, name order, surnames, seeds, region selection and errors.
// ============================================================================

const PERSONA_FIELDS = ['femaleFirstName', 'femaleMiddleName', 'femaleLastName', 'femaleFullName',
                        'maleFirstName', 'maleMiddleName', 'maleLastName', 'maleFullName'];
let REGIONS = [];

load.initialize(async function () {
  startScript();
  REGIONS = call('regions', { path: '/names/regions' }, (r, b) => {
    eq(b.count, b.regions.length, 'count matches the list'); ok(b.count >= 15, 'at least 15 regions');
    for (const x of b.regions) {
      ok(/^[A-Z]{2}$/.test(x.region), 'region code'); isStr(x.regionName, 'regionName');
      ok(x.femaleFirstNames > 0 && x.maleFirstNames > 0 && x.lastNames > 0, 'name counts');
      ok(x.combinations > 1000, 'combinations');
    }
  }).json.regions;
});

const isAscii = (s) => /^[\x20-\x7e]*$/.test(s);

function checkPersona(b, region) {
  if (region) eq(b.region, region, 'region');
  for (const f of PERSONA_FIELDS) { isStr(b[f], f); isStr(b.ascii[f], `ascii.${f}`); ok(isAscii(b.ascii[f]), `ascii.${f} is ASCII`); }
  const diacritics = PERSONA_FIELDS.some((f) => !isAscii(b[f]));
  eq(b.hasDiacritics, diacritics, 'hasDiacritics matches the names');
  if (!diacritics) PERSONA_FIELDS.forEach((f) => eq(b.ascii[f], b[f], `ascii.${f} equals ${f}`));
  for (const g of ['female', 'male']) {
    const full = b[`${g}FullName`], first = b[`${g}FirstName`], last = b[`${g}LastName`];
    if (b.familyNameFirst) ok(full.startsWith(last) && full.includes(first), `${g}: family name first`);
    else ok(full.startsWith(first) && full.endsWith(last), `${g}: given name first`);
  }
  if (!b.genderedSurname) eq(b.femaleLastName, b.maleLastName, 'the two share a surname');
}

load.action('names', async function () {
  call('service_index', { path: '/names' }, (r, b) => { ok(b.actions.includes('get') && b.actions.includes('regions'), 'actions'); });

  // Every region, with a seed so the results are checkable.
  const seed = `s${userId()}-${iteration()}`;
  for (const reg of REGIONS) {
    call('get_region', { path: '/names/get', query: { region: reg.region, seed } }, (r, b) => {
      checkPersona(b, reg.region); eq(b.regionName, reg.regionName, 'regionName');
      eq(b.familyNameFirst, reg.familyNameFirst, 'familyNameFirst as listed'); eq(b.seed, seed, 'seed echoed');
    });
  }
  // Same seed, same persona; different seeds, different personas.
  const a = call('seed_repeat_1', { path: '/names/get', query: { region: 'AU', seed: 'repeatable-42' } }).json;
  call('seed_repeat_2', { path: '/names/get', query: { region: 'au', seed: 'repeatable-42' } }, (r, b) => {
    eq(b.femaleFullName, a.femaleFullName, 'same seed same persona'); eq(b.maleFullName, a.maleFullName, 'male too');
  });
  const seen = new Set();
  for (let i = 0; i < 3; i++) {
    call('no_seed_varies', { path: '/names/get', query: { region: 'US' } }, (r, b) => { checkPersona(b, 'US'); seen.add(b.femaleFullName + b.maleFullName); });
  }
  ok(seen.size > 1, 'unseeded personas vary');

  call('region_any', { path: '/names/get', query: { region: 'ANY' } }, (r, b) => {
    ok(REGIONS.some((x) => x.region === b.region), 'a listed region'); checkPersona(b);
  });
  call('notregion_list', { path: '/names/get', query: { notregion: 'au,gb,us' } }, (r, b) => {
    ok(!['AU', 'GB', 'US'].includes(b.region), 'excluded regions not chosen');
    eq(b.excludedRegions, ['AU', 'GB', 'US'], 'excludedRegions'); eq(b.chosenFrom, REGIONS.length - 3, 'chosenFrom');
    checkPersona(b);
  });
  call('diacritics_region', { path: '/names/get', query: { region: 'PL', seed: 3 } }, (r, b) => {
    checkPersona(b, 'PL'); eq(b.genderedSurname, true, 'PL surnames are gendered');
  });

  // Errors.
  call('unknown_region_404', { path: '/names/get', query: { region: 'zz' }, expect: 404 }, (r, b) => {
    isError(r, 404, 'unknown region'); ok(b.allowed.includes('AU') && b.allowed.includes('ANY'), 'allowed listed');
  });
  call('unknown_notregion_404', { path: '/names/get', query: { notregion: 'zz' }, expect: 404 }, (r, b) => eq(b.unknown, ['ZZ'], 'unknown'));
  call('both_params_400', { path: '/names/get', query: { region: 'AU', notregion: 'GB' }, expect: 400 }, (r) => isError(r, 400, 'not both'));
  call('no_params_400', { path: '/names/get', expect: 400 }, (r, b) => { isError(r, 400); ok(Array.isArray(b.allowed), 'allowed'); });
  call('unknown_action_404', { path: '/names/random', expect: 404 }, (r) => isError(r, 404));
});
