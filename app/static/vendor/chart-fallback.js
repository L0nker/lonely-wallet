/* Lightweight offline chart fallback for Lonely Wallet.
 * Chart.js remains preferred when its integrity-verified local bundle exists.
 * This fallback supports the dashboard's line, bar, doughnut and pie charts.
 */
(function (global) {
  'use strict';
  if (global.Chart) return;

  const instances = Object.create(null);
  const defaults = { color: '#b8c1ce', borderColor: '#303541' };

  function numericValues(dataset) {
    return (dataset && Array.isArray(dataset.data) ? dataset.data : []).map(v => {
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    });
  }
  function asColor(dataset, index) {
    const colors = dataset && dataset.backgroundColor;
    if (Array.isArray(colors)) return colors[index % Math.max(colors.length, 1)] || '#6ea8e8';
    return colors || (dataset && dataset.borderColor) || '#6ea8e8';
  }
  function cssColor(value, fallback) {
    return typeof value === 'string' && value ? value : fallback;
  }

  function Chart(canvas, config) {
    if (!(this instanceof Chart)) return new Chart(canvas, config);
    this.canvas = canvas;
    this.config = config || {};
    this.type = this.config.type || 'bar';
    this.data = this.config.data || { labels: [], datasets: [] };
    this.options = this.config.options || {};
    this.id = canvas && (canvas.id || ('chart-' + Object.keys(instances).length));
    this._hits = [];
    this._boundClick = event => this._handleClick(event);
    this._boundMove = event => this._handleMove(event);
    if (canvas) {
      canvas.addEventListener('click', this._boundClick);
      canvas.addEventListener('mousemove', this._boundMove);
    }
    instances[this.id] = this;
    this.update();
  }

  Chart.defaults = defaults;
  Chart.instances = instances;

  Chart.prototype.destroy = function () {
    if (!this.canvas) return;
    this.canvas.removeEventListener('click', this._boundClick);
    this.canvas.removeEventListener('mousemove', this._boundMove);
    const ctx = this.canvas.getContext && this.canvas.getContext('2d');
    if (ctx) ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    delete instances[this.id];
  };

  Chart.prototype.update = function () {
    if (!this.canvas || !this.canvas.getContext) return;
    const canvas = this.canvas;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(240, Math.round(rect.width || canvas.clientWidth || 640));
    const height = Math.max(150, Math.round(rect.height || canvas.clientHeight || 260));
    const dpr = Math.max(1, Math.min(2, global.devicePixelRatio || 1));
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = cssColor(this.options.color, defaults.color);
    this._hits = [];
    const labels = Array.isArray(this.data.labels) ? this.data.labels : [];
    const datasets = Array.isArray(this.data.datasets) ? this.data.datasets : [];
    if (!datasets.length || !labels.length) {
      ctx.fillStyle = cssColor(this.options.color, defaults.color);
      ctx.textAlign = 'center';
      ctx.fillText('Sem dados disponíveis para o gráfico', width / 2, height / 2);
      return;
    }
    if (this.type === 'doughnut' || this.type === 'pie') {
      this._drawDoughnut(ctx, width, height, datasets[0]);
    } else {
      this._drawCartesian(ctx, width, height, labels, datasets);
    }
  };

  Chart.prototype._drawDoughnut = function (ctx, width, height, dataset) {
    const values = numericValues(dataset).map(v => Math.max(0, v || 0));
    const total = values.reduce((sum, value) => sum + value, 0);
    if (!(total > 0)) {
      ctx.fillStyle = '#7f8998';
      ctx.textAlign = 'center';
      ctx.fillText('Sem valores', width / 2, height / 2);
      return;
    }
    const cx = width / 2, cy = height / 2;
    const radius = Math.max(35, Math.min(width, height) * 0.39);
    const cutoutText = String(this.options.cutout || (this.type === 'doughnut' ? '62%' : '0%'));
    const cutoutRatio = this.type === 'pie' ? 0 : Math.max(0, Math.min(0.85, parseFloat(cutoutText) / 100 || 0));
    let angle = -Math.PI / 2;
    values.forEach((value, index) => {
      const next = angle + value / total * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, radius, angle, next);
      ctx.closePath();
      ctx.fillStyle = asColor(dataset, index);
      ctx.fill();
      this._hits.push({ datasetIndex: 0, index, start: angle, end: next, cx, cy, radius, total, value });
      angle = next;
    });
    if (cutoutRatio > 0) {
      ctx.beginPath();
      ctx.arc(cx, cy, radius * cutoutRatio, 0, Math.PI * 2);
      const parentBackground = this.canvas.parentElement ? getComputedStyle(this.canvas.parentElement).backgroundColor : '';
      ctx.fillStyle = parentBackground && parentBackground !== 'rgba(0, 0, 0, 0)' ? parentBackground : '#20232c';
      ctx.fill();
    }
  };

  Chart.prototype._drawCartesian = function (ctx, width, height, labels, datasets) {
    const left = 54, right = 14, top = 18, bottom = 34;
    const plotW = Math.max(1, width - left - right), plotH = Math.max(1, height - top - bottom);
    const values = datasets.flatMap(ds => numericValues(ds)).filter(v => v !== null);
    const scales = this.options.scales || {};
    const stacked = Boolean(scales.x && scales.x.stacked && scales.y && scales.y.stacked);
    const positiveStackTotals = labels.map((_, index) => datasets.reduce((sum, ds) => {
      const value = numericValues(ds)[index];
      return sum + (value !== null && value > 0 ? value : 0);
    }, 0));
    let min = Math.min(0, ...values);
    let max = Math.max(1, ...(stacked ? positiveStackTotals : values));
    const span = Math.max(1, max - min);
    max += span * 0.08;
    const y = value => top + (max - value) / (max - min) * plotH;
    const textColor = cssColor(this.options.color, defaults.color);
    const yTicks = scales.y && scales.y.ticks || {};
    const xTicks = scales.x && scales.x.ticks || {};
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const value = min + (max - min) * i / 4;
      const yy = y(value);
      ctx.strokeStyle = cssColor(scales.y && scales.y.grid && scales.y.grid.color, defaults.borderColor);
      ctx.globalAlpha = 0.65;
      ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(width - right, yy); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = cssColor(yTicks.color, textColor);
      const label = typeof yTicks.callback === 'function' ? yTicks.callback(value) : value.toLocaleString('pt-BR', { maximumFractionDigits: 1 });
      ctx.fillText(String(label), left - 8, yy);
    }
    const step = plotW / Math.max(1, labels.length);
    const stackPositive = Array(labels.length).fill(0);
    const groupWidth = Math.min(42, step * 0.72);
    if (this.type === 'bar') {
      datasets.forEach((dataset, datasetIndex) => {
        const vals = numericValues(dataset);
        vals.forEach((raw, index) => {
          if (raw === null) return;
          const value = raw;
          const center = left + step * (index + 0.5);
          let yBase = y(0), yTop;
          let barX = center - groupWidth / 2, barW = groupWidth;
          if (stacked) {
            yBase = y(stackPositive[index]);
            stackPositive[index] += value;
            yTop = y(stackPositive[index]);
          } else {
            const groupW = groupWidth / Math.max(1, datasets.length);
            barX = center - groupWidth / 2 + groupW * datasetIndex;
            barW = groupW * 0.86;
            yTop = y(value);
          }
          const barY = Math.min(yBase, yTop), barH = Math.max(1, Math.abs(yBase - yTop));
          ctx.fillStyle = asColor(dataset, index);
          const radius = Math.min(4, barW / 3);
          ctx.beginPath();
          if (typeof ctx.roundRect === 'function') ctx.roundRect(barX, barY, barW, barH, [radius, radius, 0, 0]);
          else ctx.rect(barX, barY, barW, barH);
          ctx.fill();
          this._hits.push({ datasetIndex, index, x: barX, y: barY, width: barW, height: barH, value, label: labels[index], datasetLabel: dataset.label });
        });
      });
    } else {
      datasets.forEach((dataset, datasetIndex) => {
        const vals = numericValues(dataset);
        ctx.beginPath();
        ctx.strokeStyle = cssColor(dataset.borderColor, asColor(dataset, 0));
        ctx.lineWidth = 2.2;
        let drawing = false;
        vals.forEach((value, index) => {
          if (value === null) { drawing = false; return; }
          const xx = left + step * (index + 0.5), yy = y(value);
          if (!drawing) { ctx.moveTo(xx, yy); drawing = true; } else ctx.lineTo(xx, yy);
          this._hits.push({ datasetIndex, index, x: xx - 8, y: yy - 8, width: 16, height: 16, value, label: labels[index], datasetLabel: dataset.label });
        });
        ctx.stroke();
        vals.forEach((value, index) => {
          if (value === null) return;
          const xx = left + step * (index + 0.5), yy = y(value);
          ctx.beginPath(); ctx.arc(xx, yy, Number(dataset.pointRadius || 3), 0, Math.PI * 2);
          ctx.fillStyle = Array.isArray(dataset.pointBackgroundColor) ? dataset.pointBackgroundColor[index] : cssColor(dataset.pointBackgroundColor, asColor(dataset, 0));
          ctx.fill();
        });
      });
    }
    const labelEvery = Math.max(1, Math.ceil(labels.length / Math.max(3, Math.floor(plotW / 62))));
    ctx.fillStyle = cssColor(xTicks.color, textColor);
    ctx.textAlign = 'center';
    labels.forEach((label, index) => {
      if (index % labelEvery !== 0 && index !== labels.length - 1) return;
      const xx = left + step * (index + 0.5);
      ctx.fillText(String(label).slice(0, 13), xx, height - 16);
    });
    const legend = this.options.plugins && this.options.plugins.legend;
    if (legend && legend.display !== false && datasets.length > 1) {
      let x = left;
      datasets.forEach(dataset => {
        ctx.fillStyle = asColor(dataset, 0);
        ctx.fillRect(x, 2, 9, 9);
        ctx.fillStyle = textColor;
        ctx.textAlign = 'left';
        const label = String(dataset.label || '').slice(0, 22);
        ctx.fillText(label, x + 13, 7);
        x += Math.max(70, ctx.measureText(label).width + 28);
      });
    }
  };

  Chart.prototype._handleClick = function (event) {
    const callback = this.options.onClick;
    if (typeof callback !== 'function') return;
    const rect = this.canvas.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    const hit = this._hits.find(item => {
      if (item.start !== undefined) {
        const dx = x - item.cx, dy = y - item.cy;
        const radius = Math.sqrt(dx * dx + dy * dy);
        if (radius > item.radius || radius < item.radius * (parseFloat(this.options.cutout || '0') / 100)) return false;
        let angle = Math.atan2(dy, dx);
        if (angle < -Math.PI / 2) angle += Math.PI * 2;
        return angle >= item.start && angle <= item.end;
      }
      return x >= item.x && x <= item.x + item.width && y >= item.y && y <= item.y + item.height;
    });
    callback(event, hit ? [{ datasetIndex: hit.datasetIndex, index: hit.index }] : [], this);
  };
  Chart.prototype._handleMove = function (event) {
    const callback = this.options.onHover;
    if (typeof callback !== 'function') return;
    const rect = this.canvas.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    const hit = this._hits.find(item => item.x !== undefined && x >= item.x && x <= item.x + item.width && y >= item.y && y <= item.y + item.height);
    callback({ native: { target: this.canvas }, x, y }, hit ? [{ datasetIndex: hit.datasetIndex, index: hit.index }] : [], this);
  };

  global.Chart = Chart;
})(window);
