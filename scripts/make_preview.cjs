const fs=require('node:fs');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const QR=require('qrcode');
const root=path.resolve(__dirname,'..');
(async()=>{
  fs.mkdirSync(path.join(root,'artifacts'),{recursive:true});
  const image=path.join(root,'artifacts/mock-qr.png');
  await QR.toFile(image,JSON.stringify({rollcallId:'123',data:'preview-only'}),{width:512,margin:4});
  execFileSync(process.env.PYTHON || 'python3',[path.join(__dirname,'preview_scanner.py'),
    '--output',path.join(root,'artifacts/scanner-preview.html'),'--qr-image',image],{stdio:'inherit'});
})().catch(error=>{console.error(error);process.exitCode=1;});
