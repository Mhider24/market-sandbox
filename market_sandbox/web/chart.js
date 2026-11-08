const svgNamespace = "http://www.w3.org/2000/svg";
const shortDate = new Intl.DateTimeFormat("en-US", {month: "short", day: "numeric", timeZone: "UTC"});
const number = new Intl.NumberFormat("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});

export function demoBars(asset, quote, session) {
  if (!quote) return [];

  let seed = quote.seed;
  function random() {
    seed = (Math.imul(1664525, seed) + 1013904223) >>> 0;
    return seed / 4294967296;
  }

  const dates = [];
  const day = new Date(session + "T00:00:00Z");
  while (dates.length < 126) {
    if (day.getUTCDay() !== 0 && day.getUTCDay() !== 6) dates.push(new Date(day));
    day.setUTCDate(day.getUTCDate() - 1);
  }

  let price = 100;
  const bars = dates.reverse().map((date, index) => {
    const open = price * (1 + (random() - .5) * .008);
    const movement = index === 125
      ? quote.change
      : .0009 + Math.sin(index / 8) * .0017 + (random() - .5) * .029;
    const close = price * (1 + movement);
    price = close;
    return {
      date,
      open,
      close,
      high: Math.max(open, close) * (1.001 + random() * .006),
      low: Math.min(open, close) * (.999 - random() * .006),
      volume: Math.round(12 + random() * 37 + Math.abs(movement) * 680),
    };
  });

  const factor = asset.close_cents / 100 / bars.at(-1).close;
  return bars.map(bar => ({
    ...bar, open: bar.open * factor, high: bar.high * factor,
    low: bar.low * factor, close: bar.close * factor,
  }));
}

function svgNode(name, attributes, text) {
  const node = document.createElementNS(svgNamespace, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

export function createChart(svg, empty, tooltip) {
  let bars = [];
  let crosshair;

  function draw() {
    empty.hidden = bars.length > 0;
    tooltip.hidden = true;
    svg.replaceChildren();
    crosshair = null;
    const {width, height} = svg.getBoundingClientRect();
    if (!bars.length || width < 100 || height < 100) return;

    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    const high = Math.max(...bars.map(bar => bar.high));
    const low = Math.min(...bars.map(bar => bar.low));
    const axisWidth = Math.max(58, number.format(high).length * 7 + 10);
    const left = 12;
    const right = width - axisWidth;
    const top = 20;
    const priceBottom = height - 73;
    const volumeBottom = height - 25;
    const barStep = (right - left) / bars.length;
    const candleWidth = Math.min(9, Math.max(1, barStep * .65));
    const padding = Math.max((high - low) * .09, high * .001);
    const yMin = low - padding;
    const yMax = high + padding;
    const volumeMax = Math.max(...bars.map(bar => bar.volume));
    const x = index => left + (index + .5) * barStep;
    const y = price => top + (yMax - price) / (yMax - yMin) * (priceBottom - top);

    for (let index = 0; index <= 5; index++) {
      const price = yMin + (yMax - yMin) * index / 5;
      const level = y(price);
      svg.append(
        svgNode("line", {x1: left, x2: right, y1: level, y2: level, class: "chart-grid"}),
        svgNode("text", {x: right + 7, y: level + 3, class: "chart-label"}, number.format(price)),
      );
    }

    const dateTicks = width < 430 ? 3 : 5;
    for (let index = 0; index < dateTicks; index++) {
      const fraction = index / (dateTicks - 1);
      const date = bars[Math.round(fraction * (bars.length - 1))].date;
      const position = left + fraction * (right - left);
      svg.append(
        svgNode("line", {x1: position, x2: position, y1: top, y2: volumeBottom, class: "chart-grid"}),
        svgNode("text", {
          x: position, y: height - 8, class: "chart-label",
          "text-anchor": index === 0 ? "start" : index === dateTicks - 1 ? "end" : "middle",
        }, shortDate.format(date)),
      );
    }

    for (const [index, bar] of bars.entries()) {
      const color = bar.close >= bar.open ? "#42a92b" : "#ff3038";
      const candleTop = Math.min(y(bar.close), y(bar.open));
      const volumeHeight = 34 * bar.volume / volumeMax;
      svg.append(
        svgNode("rect", {
          x: x(index) - candleWidth / 2, y: volumeBottom - volumeHeight,
          width: candleWidth, height: volumeHeight, fill: color, opacity: .34,
        }),
        svgNode("line", {x1: x(index), x2: x(index), y1: y(bar.high), y2: y(bar.low), stroke: color}),
        svgNode("rect", {
          x: x(index) - candleWidth / 2, y: candleTop, width: candleWidth,
          height: Math.max(1.3, Math.abs(y(bar.open) - y(bar.close))), fill: color,
        }),
      );
    }

    const last = bars.at(-1).close;
    svg.append(
      svgNode("line", {
        x1: left, x2: right, y1: y(last), y2: y(last),
        stroke: "#528dc5", "stroke-dasharray": "3 4", opacity: .65,
      }),
      svgNode("rect", {x: right + 1, y: y(last) - 9, width: axisWidth - 2, height: 17, rx: 3, fill: "#285683"}),
      svgNode("text", {x: right + 6, y: y(last) + 3, class: "chart-label last-price"}, number.format(last)),
    );

    const guide = svgNode("line", {y1: top, y2: volumeBottom, class: "chart-crosshair", visibility: "hidden"});
    svg.append(guide);
    crosshair = {guide, left, right, barStep, x};
  }

  svg.addEventListener("pointermove", event => {
    if (!crosshair) return;
    const {guide, left, right, barStep, x} = crosshair;
    const pointer = event.clientX - svg.getBoundingClientRect().left;
    if (pointer < left || pointer >= right) {
      guide.setAttribute("visibility", "hidden");
      tooltip.hidden = true;
      return;
    }
    const index = Math.min(bars.length - 1, Math.floor((pointer - left) / barStep));
    const bar = bars[index];
    guide.setAttribute("x1", x(index));
    guide.setAttribute("x2", x(index));
    guide.setAttribute("visibility", "visible");
    tooltip.textContent = [
      shortDate.format(bar.date), "O " + number.format(bar.open), "H " + number.format(bar.high),
      "L " + number.format(bar.low), "C " + number.format(bar.close),
    ].join("  ·  ");
    tooltip.hidden = false;
  });
  svg.addEventListener("pointerleave", () => {
    crosshair?.guide.setAttribute("visibility", "hidden");
    tooltip.hidden = true;
  });
  new ResizeObserver(draw).observe(svg);

  return nextBars => {
    bars = nextBars;
    draw();
  };
}
