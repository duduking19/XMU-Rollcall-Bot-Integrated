self.onmessage = ({data}) => {
  try {
    const result = jsQR(new Uint8ClampedArray(data.buffer), data.width, data.height,
      { inversionAttempts: 'attemptBoth' });
    self.postMessage({id:data.id, text:result?.data || null});
  } catch (_) { self.postMessage({id:data.id, error:true}); }
};
