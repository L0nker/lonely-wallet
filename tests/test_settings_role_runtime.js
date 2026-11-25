const fs = require('fs');
const vm = require('vm');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'app', 'templates', 'index.html'), 'utf8');
const start = html.indexOf('async function readWalletApiResponse(');
const end = html.indexOf('async function saveSecurityUsername()', start);
if (start < 0 || end < 0) throw new Error('Não foi possível localizar as funções reais de Configurações.');
const source = html.slice(start, end);

function elementsForRole(role) {
  const ids = [
    'securityPinStatus','securityPinDescription','securitySavePin','securityPinMessage',
    'securityCurrentUsername','securityAddUserPanel','securityUsernameEditor',
    'securityUsernameMessage','securityDeleteAccountDescription','securityDeleteAccountTitle','securityDeleteAccountBadge','securityDeleteAccountButton',
    'securityUsersPanel','securityAddUserMessage','securityNewUsername','securityUsernamePin'
  ];
  const map = {};
  for (const id of ids) map[id] = {textContent:'', className:'', value:'', hidden:false, dataset:{}, disabled:false};
  if (role === 'user') {
    for (const id of ['securityAddUserPanel','securityUsernameEditor','securityUsernameMessage','securityUsersPanel','securityAddUserMessage','securityNewUsername','securityUsernamePin']) map[id] = null;
  }
  return map;
}

async function run(role) {
  const map = elementsForRole(role);
  let managedUsersLoaded = 0;
  const context = {
    document: { getElementById: id => Object.prototype.hasOwnProperty.call(map, id) ? map[id] : null },
    fetch: async () => ({ ok:true, text:async()=>JSON.stringify({
      authenticated:true,
      username:'pessoa',
      role,
      is_master:role==='master',
      can_change_username:role!=='user',
      can_manage_users:role!=='user',
      other_user_count:role==='master'?2:0
    }) }),
    readWalletApiResponse: undefined,
    loadManagedUsers: async()=>{managedUsersLoaded++;},
    console
  };
  vm.createContext(context);
  vm.runInContext(source, context, {timeout:3000});
  await context.loadSecuritySettings();
  if (!map.securityCurrentUsername || map.securityCurrentUsername.value !== 'pessoa') throw new Error(`${role}: username atual não foi carregado`);
  if (role==='user') {
    if (!map.securityPinStatus.textContent.includes('Obrigatório')) throw new Error('common: status do PIN não foi carregado');
    if (managedUsersLoaded !== 0) throw new Error('common: tentou carregar gestão de usuários');
  } else if (managedUsersLoaded !== 1) {
    throw new Error(`${role}: gestão de usuários não foi carregada`);
  }
  if (role==='master') {
    if (map.securityDeleteAccountTitle.textContent !== 'Excluir conta Master') throw new Error('master: título de transferência não foi exibido');
    if (!map.securityDeleteAccountButton.textContent.includes('transferir Master') || map.securityDeleteAccountButton.disabled) throw new Error('master: ação de transferência deve estar habilitada quando há outros usuários');
  } else if (map.securityDeleteAccountButton.textContent !== 'Excluir minha conta' || map.securityDeleteAccountButton.disabled) {
    throw new Error(`${role}: autoexclusão deve permanecer disponível`);
  }
  return true;
}

(async()=>{
  for (const role of ['master','admin','user']) await run(role);
  console.log('Settings role runtime: Master/Admin/User rendered without missing-DOM errors.');
})().catch(err=>{console.error(err);process.exit(1);});
