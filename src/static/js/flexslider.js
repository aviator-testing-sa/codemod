/*
 * jQuery FlexSlider v2.0
 * http://www.woothemes.com/flexslider/
 *
 * Copyright 2012 WooThemes
 * Free to use under the GPLv2 license.
 * http://www.gnu.org/licenses/gpl-2.0.html
 *
 * Contributing author: Tyler Smith (@mbmufffin)
 */

;(function ($) {

  //FlexSlider: Object Instance
  $.flexslider = function(el, options) {
    var slider = $(el),
        vars = $.extend({}, $.flexslider.defaults, options),
        namespace = vars.namespace,
        touch = ("ontouchstart" in window) || window.DocumentTouch && document instanceof DocumentTouch,
        eventType = (touch) ? "touchend" : "click",
        vertical = vars.direction === "vertical",
        reverse = vars.reverse,
        carousel = (vars.itemWidth > 0),
        fade = vars.animation === "fade",
        asNav = vars.asNavFor !== "",
        methods = {};
    
    // Store a reference to the slider object
    $.data(el, "flexslider", slider);
    
    // Privat slider methods
    methods = {
      init: function() {
        slider.animating = false;
        slider.currentSlide = vars.startAt;
        slider.animatingTo = slider.currentSlide;
        slider.atEnd = (slider.currentSlide === 0 || slider.currentSlide === slider.last);
        slider.containerSelector = vars.selector.substr(0,vars.selector.search(' '));
        slider.slides = $(vars.selector, slider);
        slider.container = $(slider.containerSelector, slider);
        slider.count = slider.slides.length;
        // SYNC:
        slider.syncExists = $(vars.sync).length > 0;
        // SLIDE:
        if (vars.animation === "slide") vars.animation = "swing";
        slider.prop = (vertical) ? "top" : "marginLeft";
        slider.args = {};
        // SLIDESHOW:
        slider.manualPause = false;
        // TOUCH/USECSS:
        slider.transitions = !vars.video && !fade && vars.useCSS && (function() {
          var obj = document.createElement('div'),
              props = ['perspectiveProperty', 'WebkitPerspective', 'MozPerspective', 'OPerspective', 'msPerspective'];
          for (var i in props) {
            if ( obj.style[ props[i] ] !== undefined ) {
              slider.pfx = props[i].replace('Perspective','').toLowerCase();
              slider.prop = "-" + slider.pfx + "-transform";
              return true;
            }
          }
          return false;
        }());
        // CONTROLSCONTAINER:
        if (vars.controlsContainer !== "") slider.controlsContainer = $(vars.controlsContainer).length > 0 && $(vars.controlsContainer);
        // MANUAL:
        if (vars.manualControls !== "") slider.manualControls = $(vars.manualControls).length > 0 && $(vars.manualControls);
        
        // RANDOMIZE:
        if (vars.randomize) {
          slider.slides.sort(function() { return (Math.round(Math.random())-0.5); });
          slider.container.empty().append(slider.slides);
        }
        
        slider.doMath();
        
        // ASNAV:
        if (asNav) methods.asNav.setup();
        
        // INIT
        slider.setup("init");
        
        // CONTROLNAV:
        if (vars.controlNav) methods.controlNav.setup();
        
        // DIRECTIONNAV:
        if (vars.directionNav) methods.directionNav.setup();
        
        // KEYBOARD:
        if (vars.keyboard && ($(slider.containerSelector).length === 1 || vars.multipleKeyboard)) {
          $(document).bind('keyup', function(event) {
            var keycode = event.keyCode;
            if (!slider.animating && (keycode === 39 || keycode === 37)) {
              var target = (keycode === 39) ? slider.getTarget('next') :
                           (keycode === 37) ? slider.getTarget('prev') : false;
              slider.flexAnimate(target, vars.pauseOnAction);
            }
          });
        }
        // MOUSEWHEEL:
        if (vars.mousewheel) {
          slider.bind('mousewheel', function(event, delta, deltaX, deltaY) {
            event.preventDefault();
            var target = (delta < 0) ? slider.getTarget('next') : slider.getTarget('prev');
            slider.flexAnimate(target, vars.pauseOnAction);
          });
        }
        
        // PAUSEPLAY
        if (vars.pausePlay) methods.pausePlay.setup();
        
        // SLIDSESHOW
        if (vars.slideshow) {
          if (vars.pauseOnHover) {
            slider.hover(function() {
              slider.pause();
            }, function() {
              if (!slider.manualPause) slider.play();
            });
          }
          // initialize animation
          (vars.initDelay > 0) ? setTimeout(slider.play, vars.initDelay) : slider.play();
        }
        
        // TOUCH
        if (touch && vars.touch) methods.touch();
        
        // FADE&&SMOOTHHEIGHT || SLIDE:
        if (!fade || (fade && vars.smoothHeight)) $(window).bind("resize focus", methods.resize);
        
        
        // API: start() Callback
        setTimeout(function(){
          vars.start(slider);
        }, 200);
      },
      asNav: {
        setup: function() {
          slider.asNav = true;
          slider.animatingTo = Math.floor(slider.currentSlide/slider.move);
          slider.currentItem = slider.currentSlide;
          slider.slides.removeClass(namespace + "active-slide").eq(slider.currentItem).addClass(namespace + "active-slide");
          slider.slides.click(function(e){
            e.preventDefault();
            var $slide = $(this),
                target = $slide.index();
            if (!$(vars.asNavFor).data('flexslider').animating && !$slide.hasClass('active')) {
              slider.direction = (slider.currentItem < target) ? "next" : "prev";
              slider.flexAnimate(target, vars.pauseOnAction, false, true, true);
            }
          });
        }
      },
      controlNav: {
        setup: function() {
          if (!slider.manualControls) {
            methods.controlNav.setupPaging();
          } else { // MANUALCONTROLS:
            methods.controlNav.setupManual();
          }
        },
        setupPaging: function() {
          var type = (vars.controlNav === "thumbnails") ? 'control-thumbs' : 'control-paging',
              j = 1,
              item;
          
          slider.controlNavScaffold = $('<ol class="'+ namespace + 'control-nav ' + namespace + type + '"></ol>');
          
          if (slider.pagingCount > 1) {
            for (var i = 0; i < slider.pagingCount; i++) {
              item = (vars.controlNav === "thumbnails") ? '<img src="' + slider.slides.eq(i).attr("data-thumb") + '"/>' : '<a>' + j + '</a>';
              slider.controlNavScaffold.append('<li>' + item + '</li>');
              j++;
            }
          }
          
          // CONTROLSCONTAINER:
          (slider.controlsContainer) ? $(slider.controlsContainer).append(slider.controlNavScaffold) : slider.append(slider.controlNavScaffold);
          methods.controlNav.set();
          
          methods.controlNav.active();
        
          slider.controlNavScaffold.delegate('a, img', eventType, function(event) {
            event.preventDefault();
            var $this = $(this),
                target = slider.controlNav.index($this);
          
            slider.flexAnimate(target, vars.pauseOnAction);
          });
        },
        set: function() {
          var o = methods.controlNav.offsetLeft = slider.controlNavScaffold.width() ? (slider.width() - slider.controlNavScaffold.width())/2 : 0;
          if (slider.pagingCount > 1 && vars.controlNav === "thumbnails") {
            //Define Width/Height of each thumbnail control
            var thumbs = slider.controlNav.find('img');
            slider.controlNav.width((thumbs.length * thumbs.first().width()));
          }
        },
        active: function() {
          slider.controlNav.removeClass(namespace + "active").eq(slider.animatingTo).addClass(namespace + "active");
        },
        update: function(action, pos) {
          if (slider.pagingCount > 1 && action === "add") {
            slider.controlNavScaffold.append($('<li><a>'+ slider.count +'</a></li>'));
          } else if (slider.pagingCount === 1) {
            slider.controlNavScaffold.find('li').remove();
          }
          
          methods.controlNav.set();
          methods.controlNav.active();
        },
        setupManual: function() {
          slider.controlNav = slider.manualControls;
          methods.controlNav.active();
          
          slider.controlNav.bind(eventType, function(event) {
            event.preventDefault();
            var $this = $(this),
                target = slider.controlNav.index($this);
            
            slider.flexAnimate(target, vars.pauseOnAction);
          });
        }
      },
      directionNav: {
        setup: function() {
          var directionNavScaffold = $('<ul class="'+ namespace +'direction-nav"><li><a class="'+ namespace +'prev" href="#">' + vars.prevText + '</a></li><li><a class="'+ namespace +'next" href="#">' + vars.nextText + '</a></li></ul>');
          
          // CONTROLSCONTAINER:
          if (slider.controlsContainer) {
            $(slider.controlsContainer).append(directionNavScaffold);
            slider.directionNav = $(slider.controlsContainer).find('.' + namespace + 'direction-nav li a');
          } else {
            slider.append(directionNavScaffold);
            slider.directionNav = slider.find('.' + namespace + 'direction-nav li a');
          }
          
          methods.directionNav.update();
          
          slider.directionNav.bind(eventType, function(event) {
            event.preventDefault();
            var target = ($(this).hasClass(namespace +'next')) ? slider.getTarget('next') : slider.getTarget('prev');
            slider.flexAnimate(target, vars.pauseOnAction);
          });
        },
        update: function() {
          var disabledClass = namespace + 'disabled';
          if (slider.pagingCount === 1) {
            slider.directionNav.addClass(disabledClass);
          } else if (!vars.animationLoop) {
            if (slider.animatingTo === 0) {
              slider.directionNav.removeClass(disabledClass).filter('.' + namespace + 'prev').addClass(disabledClass);
            } else if (slider.animatingTo === slider.last) {
              slider.directionNav.removeClass(disabledClass).filter('.' + namespace + 'next').addClass(disabledClass);
            } else {
              slider.directionNav.removeClass(disabledClass);
            }
          } else {
            slider.directionNav.removeClass(disabledClass);
          }
        }
      },
      pausePlay: {
        setup: function() {
          var pausePlayScaffold = $('<div class="'+ namespace +'pauseplay"><a></a></div>');
          
          // CONTROLSCONTAINER:
          if (slider.controlsContainer) {
            $(slider.controlsContainer).append(pausePlayScaffold);
            slider.pausePlay = $(slider.controlsContainer).find('.' + namespace + 'pauseplay a');
          } else {
            slider.append(pausePlayScaffold);
            slider.pausePlay = slider.find('.' + namespace + 'pauseplay a');
          }
          
          methods.pausePlay.update((vars.slideshow) ? namespace + 'pause' : namespace + 'play');
          
          slider.pausePlay.bind(eventType, function(event) {
            event.preventDefault();
            if ($(this).hasClass(namespace + 'pause')) {
              slider.pause();
              slider.manualPause = true;
            } else {
              slider.play();
              slider.manualPause = false;
            }
          });
        },
        update: function(state) {
          (state === namespace +'pause') ? slider.pausePlay.removeClass(namespace + 'play').addClass(namespace + 'pause').text(vars.pauseText) : slider.pausePlay.removeClass(namespace + 'pause').addClass(namespace + 'play').text(vars.playText);
        }
      },
      touch: function() {
        var startX,
          startY,
          offset,
          cwidth,
          dx,
          dy,
          sliding = false;
        
        el.addEventListener('touchstart', function(e) {
          if (slider.animating) {
            e.preventDefault();
          } else if (e.touches.length === 1) {
            slider.pause();
            // CAROUSEL:
            if (carousel) {
              cwidth = slider.itemW;
              offset = slider.itemMargin;
            } else {
              cwidth = slider.width();
              offset = Number(slider.vars.itemMargin);
            }
            sliding = true;
            startX = e.touches[0].pageX;
            startY = e.touches[0].pageY;
          }
        }, false);
        
        el.addEventListener('touchmove', function(e) {
          if (sliding) {
            dx = e.touches[0].pageX - startX;
            dy = e.touches[0].pageY - startY;
            var max = (vertical) ? slider.height() : slider.width();
            if (Math.abs(dx) < Math.abs(dy) && vars.vertical) {
              return;
            }
            e.preventDefault();
            // CAROUSEL:
            if (carousel) {
              var panW = slider.limit - (slider.itemW + offset) * slider.move * slider.animatingTo;
              if ((dx > 0 && slider.animatingTo === 0) || (dx < 0 && panW <= 0)) {
                pull = dx / 2;
              } else {
                pull = dx / slider.move;
              }
              slider.args[slider.prop] = (reverse) ? panW - pull : (slider.animatingTo * (slider.itemW + offset) * slider.move) + pull;
            } else {
              pull = (fade) ? dx / 2 : dx / slider.move;
              slider.args[slider.prop] = slider.animatingTo * max - pull;
            }
    
            slider.setProps(slider.args);
          }
        }, false);
        
        el.addEventListener('touchend', function(e) {
          if (sliding) {
            var distance = (vertical) ? dy : dx,
              target = (distance < 0) ? slider.getTarget('next') : slider.getTarget('prev');
            
            if (Math.abs(distance) > vars.swipeThreshold) {
              slider.flexAnimate(target, vars.pauseOnAction);
            } else {
              slider.flexAnimate(slider.animatingTo, vars.pauseOnAction, true);
            }
            
            sliding = false;
          }
        }, false);
      },
      resize: function() {
        if (!slider.animating && slider.is(':visible')) {
          if (!carousel) slider.doMath();
          
          if (fade) {
            // SMOOTH HEIGHT:
            methods.smoothHeight();
          } else if (carousel) { //CAROUSEL:
            slider.slides.width(slider.computedW);
            slider.update(slider.pagingCount);
            slider.setProps({height: slider.computedH, width: slider.computedW, 'float': 'left'});
          } else {
            if (vars.animation === "slide") {
              slider.setProps({width: '100%', 'float': 'left'});
            }
            // SMOOTH HEIGHT:
            methods.smoothHeight();
          }
        }
      },
      smoothHeight: function(dur) {
        if (!vertical || fade) {
          var $obj = (fade) ? slider : slider.viewport;
          (dur === undefined) ? $obj.height(slider.slides.eq(slider.animatingTo).height()) : $obj.animate({height: slider.slides.eq(slider.animatingTo).height()}, dur);
        }
      },
      sync: function(action) {
        var $obj = $(vars.sync).data("flexslider"),
            target = slider.animatingTo;
        
        switch (action) {
          case "animate":    $obj.flexAnimate(target, vars.pauseOnAction, false, true);   break;
          case "play":       if (!$obj.playing && !$obj.asNav) { $obj.play(); }          break;
          case "pause":      $obj.pause();                                                break;
        }
      }
    };
    
    // public methods
    slider.flexAnimate = function(target, pause, override, withSync, fromNav) {
      if (!slider.animating && slider.is(':visible')) {
        if (!slider.animating) {
          slider.animating = true;
          slider.animatingTo = target;
          
          // API: before() animation Callback
          vars.before(slider);
          
          // SYNC:
          if (withSync) methods.sync("animate");
          
          // SLIDE:
          if (!vars.controlNav && fromNav === false) slider.direction = (target > slider.currentSlide) ? "next" : "prev";
          
          // FRACTIONALPIXEL:
          if (slider.reverse) {
            if(target > slider.currentSlide) {
              slider.direction = "prev";
            } else {
              slider.direction = "next";
            }
          }
            
          // SLIDE:
          if (!fade || !touch) slider.pause();
          
          // ASNAV:
          if (vars.controlNav) methods.controlNav.active();
          
          // SMOOTH HEIGHT:
          if (vars.smoothHeight) methods.smoothHeight(vars.animationSpeed);
          
          if (slider.transitions) {
            var dimension = (vertical) ? "translate3d(0,-" + target * slider.height() + "px,0)" : "translate3d(-" + target * slider.width() + "px,0,0)";
            slider.setProps({过渡: dimension, 持续时间: vars.animationSpeed + "ms", 延迟: "0ms", timingFunction: vars.easing});
            
            // WITH REALLY REALLY OLD CSS SUPPORT
            var trans = "translate3d(-" + target * slider.width() + "px,0,0)";
              slider.setProps({'-o-transform': trans});
              slider.setProps({'-ms-transform': trans});
              slider.setProps({'-moz-transform': trans});
              slider.setProps({'-webkit-transform': trans});
              slider.setProps({'transform': trans});
              slider.setProps({'-o-transition-duration': vars.animationSpeed + "ms"});
              slider.setProps({'-ms-transition-duration': vars.animationSpeed + "ms"});
              slider.setProps({'-moz-transition-duration': vars.animationSpeed + "ms"});
              slider.setProps({'-webkit-transition-duration': vars.animationSpeed + "ms"});
              slider.setProps({'transition-duration': vars.animationSpeed + "ms"});
          }
          
          slider.animating = true;
          
          // SLIDE:
          slider.currentSlide = target;
          
          // API: after() animation Callback
          setTimeout(function() {
            slider.animating = false;
            // API: after() animation Callback
            vars.after(slider);
          }, vars.animationSpeed);
          
          // SLIDE:
          if (vars.pauseOnAction && !pause) slider.pause();
        }
      }
    };
    slider.pause = function() {
      slider.playing = false;
      clearInterval(slider.interval);
      slider.interval = null;
      // SYNC:
      if (vars.pausePlay) methods.pausePlay.update(namespace + 'play');
      // SYNC:
      if (methods.sync) methods.sync("pause");
    };
    slider.play = function() {
      slider.playing = true;
      slider.interval = setInterval(function() {
        var target = slider.getTarget('next');
        slider.flexAnimate(target, false);
      }, vars.slideshowSpeed);
      // SYNC:
      if (vars.pausePlay) methods.pausePlay.update(namespace + 'pause');
      // SYNC:
      if (methods.sync) methods.sync("play");
    };
    slider.canAdvance = function(target, pause, override, withSync, fromNav) {
      if (!slider.animating && slider.is(':visible')) {
        return true;
      }
      return false;
    };
    slider.getTarget = function(dir) {
      slider.direction = dir;
      if (dir === "next") {
        return (slider.currentSlide === slider.last) ? 0 : slider.currentSlide + 1;
      } else {
        return (slider.currentSlide === 0) ? slider.last : slider.currentSlide - 1;
      }
    };
    
    // SLIDE:
    slider.setProps = function(props) {
      var slider = this,
        propSet = function(prop) {
          if (slider.transitions) {
            slider.container.css({
              "-o-transition-property": prop,
              "-ms-transition-property": prop,
              "-moz-transition-property": prop,
              "-webkit-transition-property": prop,
              "transition-property": prop
            });
            slider.container.css(props);
          }
        };
      
      if (props.to过度 !== undefined) {
          propSet("all");
      } else {
          for (var prop in props) {
              propSet(prop);
          }
      }
    };
    
    slider.setup = function(type) {
      // SLIDE:
      if (!fade) {
        var sliderWidth, sliderHeight;
        if (type === "init") {
          slider.viewport = $('<div class="'+ namespace +'viewport"></div>').css({overflow: 'hidden', position: 'relative'}).appendTo(slider).append(slider.container);
          // INFINITE LOOP:
          slider.cloneCount = 0;
          if (vars.animationLoop) {
            slider.cloneCount = 2;
            slider.container.append(slider.slides.first().clone().addClass(namespace + 'clone')).prepend(slider.slides.last().clone().addClass(namespace + 'clone'));
          }
          // /INFINITE LOOP:
          
          slider.newSlides = $(vars.selector, slider);
          
          sliderWidth = slider.width();
          sliderHeight = slider.height();
          
          slider.computedW = sliderWidth - (Number(slider.vars.itemMargin));
          slider.computedH = sliderHeight;
          
          slider.slides.width(slider.computedW);
          slider.container.width((slider.count + slider.cloneCount) * slider.computedW);
          slider.setProps({width: (slider.count + slider.cloneCount) * slider.computedW, marginLeft: slider.computedW * -1});
        
        } else if (type === "update") {
          sliderWidth = slider.width();
          sliderHeight = slider.height();
          
          slider.computedW = sliderWidth - (Number(slider.vars.itemMargin));
          slider.computedH = sliderHeight;
          
          slider.update(slider.pagingCount);
          slider.slides.width(slider.computedW);
          slider.container.width((slider.count + slider.cloneCount) * slider.computedW);
          slider.setProps({marginLeft: slider.computedW * -1});
        }
      }
      
      //vertical
      if (vertical) {
        slider.viewport.height(slider.height());
        slider.setProps({height: sliderHeight, width: sliderWidth});
      }
      
      slider.slides.show();
      
      // SMOOTH HEIGHT:
      if (vars.smoothHeight) methods.smoothHeight();
    };
    
    //CAROUSEL:
    slider.doMath = function() {
      var slideWidth = slider.slides.first().width() + slider.vars.itemMargin,
          offset = slider.vars.itemMargin,
          minItems;
      
      slider.w = slideWidth;
      slider.h = slider.slides.first().height();
      
      slider.boxW = slider.slides.first().width();
      slider.boxH = slider.slides.first().height();
      
      // CAROUSEL:
      slider.itemW = slideWidth + offset;
      
      slider.move = vars.move;
      slider.pagingCount = Math.max(Math.ceil(slider.count / slider.move), 1);
      slider.last = slider.pagingCount - 1;
      slider.limit = (slider.pagingCount === 1) ? 0 :
                     (vars.itemWidth > slider.width()) ? (slider.pagingCount - 1) * slideWidth : (slider.count - vars.minItems) * slideWidth;
    };
    
    slider.update = function(pos) {
      slider.doMath();
      
      // CONTROLNAV:
      if (vars.controlNav) methods.controlNav.update();
      // DIRECTIONNAV:
      if (vars.directionNav) methods.directionNav.update();
      
      // ASNAV:
      if (asNav) {
        slider.animatingTo = Math.floor(pos/slider.move);
        slider.currentItem = pos;
        slider.slides.removeClass(namespace + "active-slide").eq(slider.currentItem).addClass(namespace + "active-slide");
      }
    };
    
    // GO!
    methods.init();
  };
  
  // Ensure the slider isn't focussed (blurry issue in FF)
  $(window).blur(function (e) {
    try {
      document.activeElement.blur();
    } catch (e) { }
  });
  
  // FlexSlider: Default Settings
  $.flexslider.defaults = {
    namespace: "flex-",               //{NEW} String: Prefix string attached to the class of every element generated by the plugin
    selector: ".slides > li",        //{NEW} String: Must match a simple pattern. '{container} > {slide}' -- Ignore pattern at your own peril
    easing: "swing",               //{NEW} String: Determines the easing method used for transitions. jQuery easing plugin is supported!
    animation: "fade",              //String: Select your animation type, "fade" or "slide"
    smoothHeight:false,             //{NEW} Boolean: Allow height of the slider to animate smoothly in horizontal mode
    reverse: false,               //{NEW} Boolean: Allows the slider to move in a opposite direction
    animationLoop: true,             //Boolean: Should the animation loop? If false, directionNav will be disabled when at the first/last slide.
    startAt: 0,                  //Integer: The slide that the slider should start on. Array notation (0 = first slide)
    itemWidth: 0,                   //{NEW} Integer: Box-model width of individual carousel items, more for carousel purposes
    itemMargin: 0,                  //{NEW} Integer: Margin between carousel items, more for carousel purposes
    minItems: 0,                  //{NEW} Integer: Minimum number of carousel items that should be visible. Items will resize fluidly when below this.
    maxItems: 0,                  //{NEW} Integer: Maxmimum number of carousel items that should be visible. Items will resize fluidly when above this.
    move: 1,                  //{NEW} Integer: Number of carousel items that should move on animation. If 0, slider will move all visible items.
    animationSpeed: 600,              //Integer: Set the speed of animations
    slideshowSpeed: 7000,              //Integer: Set the speed of the slideshow cycling
    pauseOnAction: true,              //Boolean: Pause the slideshow when the user interacts with the control elements.
    pauseOnHover: false,              //Boolean: Pause the slideshow when hovering over the slider
    initDelay: 0,                  //Integer: Set a delay for the slideshow to start when the page is loaded.
    useCSS: true,               //{NEW} Boolean: Slider will use CSS3 transitions if available
    touch: true,               //{NEW} Boolean: Allow touch swipe navigation of the slider on touch-enabled devices
    swipeThreshold: 0,                  //{NEW} Integer: Define the amount of the pixels that the touch event has to move vefore move the slider. If you use a number lower than 0, the slider will move regardles of the distance.
    video: false,               //{NEW} Boolean: If using video in the slider, will prevent CSS3 3D Transforms to avoid graphical glitches
    
    // Usability features
    keyboard: true,               //Boolean: Allow slider navigating via keyboard left/right keys
    multipleKeyboard: false,            //{NEW} Boolean: Allow keyboard navigation to affect multiple sliders. Default false.
    mousewheel: false,              //{NEW} Boolean: Requires jquery.mousewheel.js (https://github.com/brandonaaron/jquery-mousewheel) - Allows slider navigating via mousewheel
    pausePlay: false,               //{NEW} Boolean: Create pause/play dynamic element
    pauseText: 'Pause',             //{NEW} String: Set the pause text
    playText: 'Play',               //{NEW} String: Set the play text
    
    // Navigation
    controlNav: true,              //Boolean: Create navigation for paging control of each slide? Note: Leave true for slideshows made up of one photo.
    directionNav: true,              //Boolean: Create navigation for previous/next navigation? (true/false)
    prevText: 'Previous',             //String: Set the text for the "previous" directionNav item
    nextText: 'Next',               //String: Set the text for the "next" directionNav item
    
    // Selectors
    controlsContainer: "",             //{NEW} Selector: jQuery selector that specifies the container of the navigation. If false, the navigation will be appended to the slider before the viewport.
    manualControls: "",             //{NEW} Selector: jQuery selector that specifies the container of the manual controls.
    sync: "",                   //{NEW} Selector: Mirror the actions performed on this slider with another slider. Use with care.
    asNavFor: "",                   //{NEW} Selector: Internal property exposed for use as a navigation slider (See ASNAV section above)
    
    // Carousel Only
    itemWidth: 0,                   //{NEW} Integer: Box-model width of individual carousel items, more for carousel purposes
    itemMargin: 0,                  //{NEW} Integer: Margin between carousel items, more for carousel purposes
    minItems: 0,                  //{NEW} Integer: Minimum number of carousel items that should be visible. Items will resize fluidly when below this.
    maxItems: 0,                  //{NEW} Integer: Maxmimum number of carousel items that should be visible. Items will resize fluidly when above this.
    move: 0,                  //{NEW} Integer: Number of carousel items that should move on animation. If 0, slider will move all visible items.
    
    // Callback API
    start: function(){},            //Callback: function(slider) - Fires when the slider loads the first slide
    before: function(){},           //Callback: function(slider) - Fires asynchronously before each animation starts
    after: function(){}            //Callback: function(slider) - Fires after each animation completes
  }
  
  // FlexSlider: Plugin Function
  $.fn.flexslider = function(options) {
    if (options === undefined) options = {};
    
    if (typeof options === "object") {
      return this.each(function() {
        var $this = $(this),
            existingSlider = $this.data("flexslider");
        if (!existingSlider) {
          new $.flexslider(this, options);
        }
      });
    } else {
      var method = options;
      return this.each(function() {
        var $this = $(this),
            slider = $this.data("flexslider");
        if (slider) {
          if (typeof slider[method] === "function") {
            slider[method]();
          } else {
            throw new Error("FlexSlider: Method " + method + " does not exist.");
          }
        }
      });
    }
  };
  
  // FlexSlider: Icon String
  //   $.flexslider.icon = '<use xlink:href="#icon-arrow-left" href="#icon-arrow-left"></use>';
})(jQuery);

            if (!$this.hasClass(namespace + 'active')) {
              slider.direction = (target > slider.currentSlide) ? "next" : "prev";
              slider.flexAnimate(target, vars.pauseOnAction);
            }
          });
          // Prevent iOS click event bug
          if (touch) {
            slider.controlNavScaffold.delegate('a', "click touchstart", function(event) {
              event.preventDefault();
            });
          }
        },
        setupManual: function() {
          slider.controlNav = slider.manualControls;
          methods.controlNav.active();
          
          slider.controlNav.on(eventType, function(event) {
            event.preventDefault();
            var $this = $(this),
                target = slider.controlNav.index($this);
                
            if (!$this.hasClass(namespace + 'active')) {
              (target > slider.currentSlide) ? slider.direction = "next" : slider.direction = "prev";
              slider.flexAnimate(target, vars.pauseOnAction);
            }
          });
          // Prevent iOS click event bug
          if (touch) {
            slider.controlNav.on("click touchstart", function(event) {
              event.preventDefault();
            });
          }
        },
        set: function() {
          var selector = (vars.controlNav === "thumbnails") ? 'img' : 'a';
          slider.controlNav = $('.' + namespace + 'control-nav li ' + selector, (slider.controlsContainer) ? slider.controlsContainer : slider);
        },
        active: function() {
          slider.controlNav.removeClass(namespace + "active").eq(slider.animatingTo).addClass(namespace + "active");
        },
        update: function(action, pos) {
          if (slider.pagingCount > 1 && action === "add") {
            slider.controlNavScaffold.append($('<li><a>' + slider.count + '</a></li>'));
          } else if (slider.pagingCount === 1) {
            slider.controlNavScaffold.find('li').remove();
          } else {
            slider.controlNav.eq(pos).closest('li').remove();
          }
          methods.controlNav.set();
          (slider.pagingCount > 1 && slider.pagingCount !== slider.controlNav.length) ? slider.update(pos, action) : methods.controlNav.active();
        }
      },
      directionNav: {
        setup: function() {
          var directionNavScaffold = $('<ul class="' + namespace + 'direction-nav"><li><a class="' + namespace + 'prev" href="#">' + vars.prevText + '</a></li><li><a class="' + namespace + 'next" href="#">' + vars.nextText + '</a></li></ul>');
        
          // CONTROLSCONTAINER:
          if (slider.controlsContainer) {
            $(slider.controlsContainer).append(directionNavScaffold);
            slider.directionNav = $('.' + namespace + 'direction-nav li a', slider.controlsContainer);
          } else {
            slider.append(directionNavScaffold);
            slider.directionNav = $('.' + namespace + 'direction-nav li a', slider);
          }
        
          methods.directionNav.update();
        
          slider.directionNav.on(eventType, function(event) {
            event.preventDefault();
            var target = ($(this).hasClass(namespace + 'next')) ? slider.getTarget('next') : slider.getTarget('prev');
            slider.flexAnimate(target, vars.pauseOnAction);
          });
          // Prevent iOS click event bug
          if (touch) {
            slider.directionNav.on("click touchstart", function(event) {
              event.preventDefault();
            });
          }
        },
        update: function() {
          var disabledClass = namespace + 'disabled';
          if (!vars.animationLoop) {
            if (slider.pagingCount === 1) {
             slider.directionNav.addClass(disabledClass);
            } else if (slider.animatingTo === 0) {
              slider.directionNav.removeClass(disabledClass).filter('.' + namespace + "prev").addClass(disabledClass);
            } else if (slider.animatingTo === slider.last) {
              slider.directionNav.removeClass(disabledClass).filter('.' + namespace + "next").addClass(disabledClass);
            } else {
              slider.directionNav.removeClass(disabledClass);
            }
          }
        }
      },
      pausePlay: {
        setup: function() {
          var pausePlayScaffold = $('<div class="' + namespace + 'pauseplay"><a></a></div>');
        
          // CONTROLSCONTAINER:
          if (slider.controlsContainer) {
            slider.controlsContainer.append(pausePlayScaffold);
            slider.pausePlay = $('.' + namespace + 'pauseplay a', slider.controlsContainer);
          } else {
            slider.append(pausePlayScaffold);
            slider.pausePlay = $('.' + namespace + 'pauseplay a', slider);
          }
        
          // slider.pausePlay.addClass(pausePlayState).text((pausePlayState == 'pause') ? vars.pauseText : vars.playText);
          methods.pausePlay.update((vars.slideshow) ? namespace + 'pause' : namespace + 'play');
        
          slider.pausePlay.on(eventType, function(event) {
            event.preventDefault();
            if ($(this).hasClass(namespace + 'pause')) {
              slider.pause();
              slider.manualPause = true;
            } else {
              slider.play();
              slider.manualPause = false;
            }
          });
          // Prevent iOS click event bug
          if (touch) {
            slider.pausePlay.on("click touchstart", function(event) {
              event.preventDefault();
            });
          }
        },
        update: function(state) {
          (state === "play") ? slider.pausePlay.removeClass(namespace + 'pause').addClass(namespace + 'play').text(vars.playText) : slider.pausePlay.removeClass(namespace + 'play').addClass(namespace + 'pause').text(vars.pauseText);
        }
      },
      touch: function() {
        var startX,
          startY,
          offset,
          cwidth,
          dx,
          startT,
          scrolling = false;
              
        el.addEventListener('touchstart', onTouchStart, false);
        function onTouchStart(e) {
          if (slider.animating) {
            e.preventDefault();
          } else if (e.touches.length === 1) {
            slider.pause();
            // CAROUSEL: 
            cwidth = (vertical) ? slider.h : slider. w;
            startT = Number(new Date());
            // CAROUSEL:
            offset = (carousel && reverse && slider.animatingTo === slider.last) ? 0 :
                     (carousel && reverse) ? slider.limit - (((slider.itemW + vars.itemMargin) * slider.move) * slider.animatingTo) :
                     (carousel && slider.currentSlide === slider.last) ? slider.limit :
                     (carousel) ? ((slider.itemW + vars.itemMargin) * slider.move) * slider.currentSlide : 
                     (reverse) ? (slider.last - slider.currentSlide + slider.cloneOffset) * cwidth : (slider.currentSlide + slider.cloneOffset) * cwidth;
            startX = (vertical) ? e.touches[0].pageY : e.touches[0].pageX;
            startY = (vertical) ? e.touches[0].pageX : e.touches[0].pageY;

            el.addEventListener('touchmove', onTouchMove, false);
            el.addEventListener('touchend', onTouchEnd, false);
          }
        }

        function onTouchMove(e) {
          dx = (vertical) ? startX - e.touches[0].pageY : startX - e.touches[0].pageX;
          scrolling = (vertical) ? (Math.abs(dx) < Math.abs(e.touches[0].pageX - startY)) : (Math.abs(dx) < Math.abs(e.touches[0].pageY - startY));
          
          if (!scrolling || Number(new Date()) - startT > 500) {
            e.preventDefault();
            if (!fade && slider.transitions) {
              if (!vars.animationLoop) {
                dx = dx/((slider.currentSlide === 0 && dx < 0 || slider.currentSlide === slider.last && dx > 0) ? (Math.abs(dx)/cwidth+2) : 1);
              }
              slider.setProps(offset + dx, "setTouch");
            }
          }
        }
        
        function onTouchEnd(e) {
          if (slider.animatingTo === slider.currentSlide && !scrolling && !(dx === null)) {
            var updateDx = (reverse) ? -dx : dx,
                target = (updateDx > 0) ? slider.getTarget('next') : slider.getTarget('prev');
            
            if (slider.canAdvance(target) && (Number(new Date()) - startT < 550 && Math.abs(updateDx) > 20 || Math.abs(updateDx) > cwidth/2)) {
              slider.flexAnimate(target, vars.pauseOnAction);
            } else {
              slider.flexAnimate(slider.currentSlide, vars.pauseOnAction, true);
            }
          }
          // finish the touch by undoing the touch session
          el.removeEventListener('touchmove', onTouchMove, false);
          el.removeEventListener('touchend', onTouchEnd, false);
          startX = null;
          startY = null;
          dx = null;
          offset = null;
        }
      },
      resize: function() {
        if (!slider.animating && slider.is(':visible')) {
          if (!carousel) slider.doMath();
          
          if (fade) {
            // SMOOTH HEIGHT:
            methods.smoothHeight();
          } else if (carousel) { //CAROUSEL:
            slider.slides.width(slider.computedW);
            slider.update(slider.pagingCount);
            slider.setProps();
          }
          else if (vertical) { //VERTICAL:
            slider.viewport.height(slider.h);
            slider.setProps(slider.h, "setTotal");
          } else {
            // SMOOTH HEIGHT:
            if (vars.smoothHeight) methods.smoothHeight();
            slider.newSlides.width(slider.computedW);
            slider.setProps(slider.computedW, "setTotal");
          }
        }
      },
      smoothHeight: function(dur) {
        if (!vertical || fade) {
          var $obj = (fade) ? slider : slider.viewport;
          (dur) ? $obj.animate({"height": slider.slides.eq(slider.animatingTo).height()}, dur) : $obj.height(slider.slides.eq(slider.animatingTo).height());
        }
      },
      sync: function(action) {
        var $obj = $(vars.sync).data("flexslider"),
            target = slider.animatingTo;
        
        switch (action) {
          case "animate": $obj.flexAnimate(target, vars.pauseOnAction, false, true); break;
          case "play": if (!$obj.playing && !$obj.asNav) { $obj.play(); } break;
          case "pause": $obj.pause(); break;
        }
      }
    }
    
    // public methods
    slider.flexAnimate = function(target, pause, override, withSync, fromNav) {
      if (!slider.animating && (slider.canAdvance(target) || override) && slider.is(":visible")) {
        if (asNav && withSync) {
          var master = $(vars.asNavFor).data('flexslider');
          slider.atEnd = target === 0 || target === slider.count - 1;
          master.flexAnimate(target, true, false, true, fromNav);
          slider.direction = (slider.currentItem < target) ? "next" : "prev";
          master.direction = slider.direction;
          
          if (Math.ceil((target + 1)/slider.visible) - 1 !== slider.currentSlide && target !== 0) {
            slider.currentItem = target;
            slider.slides.removeClass(namespace + "active-slide").eq(target).addClass(namespace + "active-slide");
            target = Math.floor(target/slider.visible);
          } else {
            slider.currentItem = target;
            slider.slides.removeClass(namespace + "active-slide").eq(target).addClass(namespace + "active-slide");
            return false;
          }
        }
        
        slider.animating = true;
        slider.animatingTo = target;
        // API: before() animation Callback
        vars.before(slider);
        
        // SLIDESHOW:
        if (pause) slider.pause();
        
        // SYNC:
        if (slider.syncExists && !fromNav) methods.sync("animate");
        
        // CONTROLNAV
        if (vars.controlNav) methods.controlNav.active();
        
        // !CAROUSEL:
        // CANDIDATE: slide active class (for add/remove slide)
        if (!carousel) slider.slides.removeClass(namespace + 'active-slide').eq(target).addClass(namespace + 'active-slide');
        
        // INFINITE LOOP:
        // CANDIDATE: atEnd
        slider.atEnd = target === 0 || target === slider.last;
        
        // DIRECTIONNAV:
        if (vars.directionNav) methods.directionNav.update();
        
        if (target === slider.last) {
          // API: end() of cycle Callback
          vars.end(slider);
          // SLIDESHOW && !INFINITE LOOP:
          if (!vars.animationLoop) slider.pause();
        }
        
        // SLIDE:
        if (!fade) {
          var dimension = (vertical) ? slider.slides.filter(':first').height() : slider.computedW,
              margin, slideString, calcNext;
          
          // INFINITE LOOP / REVERSE:
          if (carousel) {
            margin = (vars.itemWidth > slider.w) ? vars.itemMargin * 2 : vars.itemMargin;
            calcNext = ((slider.itemW + margin) * slider.move) * slider.animatingTo;
            slideString = (calcNext > slider.limit && slider.visible !== 1) ? slider.limit : calcNext;
          } else if (slider.currentSlide === 0 && target === slider.count - 1 && vars.animationLoop && slider.direction !== "next") {
            slideString = (reverse) ? (slider.count + slider.cloneOffset) * dimension : 0;
          } else if (slider.currentSlide === slider.last && target === 0 && vars.animationLoop && slider.direction !== "prev") {
            slideString = (reverse) ? 0 : (slider.count + 1) * dimension;
          } else {
            slideString = (reverse) ? ((slider.count - 1) - target + slider.cloneOffset) * dimension : (target + slider.cloneOffset) * dimension;
          }
          slider.setProps(slideString, "", vars.animationSpeed);
          if (slider.transitions) {
            if (!vars.animationLoop || !slider.atEnd) {
              slider.animating = false;
              slider.currentSlide = slider.animatingTo;
            }
            slider.container.unbind("webkitTransitionEnd transitionend");
            slider.container.bind("webkitTransitionEnd transitionend", function() {
              slider.wrapup(dimension);
            });
          } else {
            slider.container.animate(slider.args, vars.animationSpeed, vars.easing, function(){
              slider.wrapup(dimension);
            });
          }
        } else { // FADE:
          slider.slides.eq(slider.currentSlide).fadeOut(vars.animationSpeed, vars.easing);
          slider.slides.eq(target).fadeIn(vars.animationSpeed, vars.easing, slider.wrapup);
        }
        // SMOOTH HEIGHT:
        if (vars.smoothHeight) methods.smoothHeight(vars.animationSpeed);
      }
    } 
    slider.wrapup = function(dimension) {
      // SLIDE:
      if (!fade && !carousel) {
        if (slider.currentSlide === 0 && slider.animatingTo === slider.last && vars.animationLoop) {
          slider.setProps(dimension, "jumpEnd");
        } else if (slider.currentSlide === slider.last && slider.animatingTo === 0 && vars.animationLoop) {
          slider.setProps(dimension, "jumpStart");
        }
      }
      slider.animating = false;
      slider.currentSlide = slider.animatingTo;
      // API: after() animation Callback
      vars.after(slider);
    }
    
    // SLIDESHOW:
    slider.animateSlides = function() {
      if (!slider.animating) slider.flexAnimate(slider.getTarget("next"));
    }
    // SLIDESHOW:
    slider.pause = function() {
      clearInterval(slider.animatedSlides);
      slider.playing = false;
      // PAUSEPLAY:
      if (vars.pausePlay) methods.pausePlay.update("play");
      // SYNC:
      if (slider.syncExists) methods.sync("pause");
    }
    // SLIDESHOW:
    slider.play = function() {
      slider.animatedSlides = setInterval(slider.animateSlides, vars.slideshowSpeed);
      slider.playing = true;
      // PAUSEPLAY:
      if (vars.pausePlay) methods.pausePlay.update("pause");
      // SYNC:
      if (slider.syncExists) methods.sync("play");
    }
    slider.canAdvance = function(target) {
      // ASNAV:
      var last = (asNav) ? slider.pagingCount - 1 : slider.last;
      return (asNav && slider.currentItem === 0 && target === slider.pagingCount - 1 && slider.direction !== "next") ? false :
             (target === slider.currentSlide && !asNav) ? false :
             (vars.animationLoop) ? true :
             (slider.atEnd && slider.currentSlide === 0 && target === last && slider.direction !== "next") ? false :
             (slider.atEnd && slider.currentSlide === last && target === 0 && slider.direction === "next") ? false :
             true;
    }
    slider.getTarget = function(dir) {
      slider.direction = dir; 
      if (dir === "next") {
        return (slider.currentSlide === slider.last) ? 0 : slider.currentSlide + 1;
      } else {
        return (slider.currentSlide === 0) ? slider.last : slider.currentSlide - 1;
      }
    }
    
    // SLIDE:
    slider.setProps = function(pos, special, dur) {
      var target = (function() {
        var posCheck = (pos) ? pos : ((slider.itemW + vars.itemMargin) * slider.move) * slider.animatingTo,
            posCalc = (function() {
              if (carousel) {
                return (special === "setTouch") ? pos :
                       (reverse && slider.animatingTo === slider.last) ? 0 :
                       (reverse) ? slider.limit - (((slider.itemW + vars.itemMargin) * slider.move) * slider.animatingTo) :
                       (slider.animatingTo === slider.last) ? slider.limit : posCheck;
              } else {
                switch (special) {
                  case "setTotal": return (reverse) ? ((slider.count - 1) - slider.currentSlide + slider.cloneOffset) * pos : (slider.currentSlide + slider.cloneOffset) * pos;
                  case "setTouch": return (reverse) ? pos : pos;
                  case "jumpEnd": return (reverse) ? pos : slider.count * pos;
                  case "jumpStart": return (reverse) ? slider.count * pos : pos;
                  default: return pos;
                }
              }
            }());
            return (posCalc * -1) + "px";
          }());

      if (slider.transitions) {
        target = (vertical) ? "translate3d(0," + target + ",0)" : "translate3d(" + target + ",0,0)";
        dur = (dur !== undefined) ? (dur/1000) + "s" : "0s";
        slider.container.css("-" + slider.pfx + "-transition-duration", dur);
      }
      
      slider.args[slider.prop] = target;
      if (slider.transitions || dur === undefined) slider.container.css(slider.args);
    }
    
    slider.setup = function(type) {
      // SLIDE:
      if (!fade) {
        var sliderOffset, arr;
            
        if (type === "init") {
          slider.viewport = $('<div class="flex-viewport"></div>').css({"overflow": "hidden", "position": "relative"}).appendTo(slider).append(slider.container);
          // INFINITE LOOP:
          slider.cloneCount = 0;
          slider.cloneOffset = 0;
          // REVERSE:
          if (reverse) {
            arr = $.makeArray(slider.slides).reverse();
            slider.slides = $(arr);
            slider.container.empty().append(slider.slides);
          }
        }
        // INFINITE LOOP && !CAROUSEL:
        if (vars.animationLoop && !carousel) {
          slider.cloneCount = 2;
          slider.cloneOffset = 1;
          // clear out old clones
          if (type !== "init") slider.container.find('.clone').remove();
          slider.container.append(slider.slides.first().clone().addClass('clone')).prepend(slider.slides.last().clone().addClass('clone'));
        }
        slider.newSlides = $(vars.selector, slider);
        
        sliderOffset = (reverse) ? slider.count - 1 - slider.currentSlide + slider.cloneOffset : slider.currentSlide + slider.cloneOffset;
        // VERTICAL:
        if (vertical && !carousel) {
          slider.container.height((slider.count + slider.cloneCount) * 200 + "%").css("position", "absolute").width("100%");
          setTimeout(function(){
            slider.newSlides.css({"display": "block"});
            slider.doMath();
            slider.viewport.height(slider.h);
            slider.setProps(sliderOffset * slider.h, "init");
          }, (type === "init") ? 100 : 0);
        } else {
          slider.container.width((slider.count + slider.cloneCount) * 200 + "%");
          slider.setProps(sliderOffset * slider.computedW, "init");
          setTimeout(function(){
            slider.doMath();
            slider.newSlides.css({"width": slider.computedW, "float": "left", "display": "block"});
            // SMOOTH HEIGHT:
            if (vars.smoothHeight) methods.smoothHeight();
          }, (type === "init") ? 100 : 0);
        }
      } else { // FADE: 
        slider.slides.css({"width": "100%", "float": "left", "marginRight": "-100%", "position": "relative"});
        if (type === "init") slider.slides.eq(slider.currentSlide).fadeIn(vars.animationSpeed, vars.easing);
        // SMOOTH HEIGHT:
        if (vars.smoothHeight) methods.smoothHeight();
      }
      // !CAROUSEL:
      // CANDIDATE: active slide
      if (!carousel) slider.slides.removeClass(namespace + "active-slide").eq(slider.currentSlide).addClass(namespace + "active-slide");
    }
    
    slider.doMath = function() {
      var slide = slider.slides.first(),
          slideMargin = vars.itemMargin,
          minItems = vars.minItems,
          maxItems = vars.maxItems;
      
      slider.w = slider.width();
      slider.h = slide.height();
      slider.boxPadding = slide.outerWidth() - slide.width();

      // CAROUSEL:
      if (carousel) {
        slider.itemT = vars.itemWidth + slideMargin;
        slider.minW = (minItems) ? minItems * slider.itemT : slider.w;
        slider.maxW = (maxItems) ? maxItems * slider.itemT : slider.w;
        slider.itemW = (slider.minW > slider.w) ? (slider.w - (slideMargin * minItems))/minItems :
                       (slider.maxW < slider.w) ? (slider.w - (slideMargin * maxItems))/maxItems :
                       (vars.itemWidth > slider.w) ? slider.w : vars.itemWidth;
        slider.visible = Math.floor(slider.w/(slider.itemW + slideMargin));
        slider.move = (vars.move > 0 && vars.move < slider.visible ) ? vars.move : slider.visible;
        slider.pagingCount = Math.ceil(((slider.count - slider.visible)/slider.move) + 1);
        slider.last =  slider.pagingCount - 1;
        slider.limit = (slider.pagingCount === 1) ? 0 :
                       (vars.itemWidth > slider.w) ? ((slider.itemW + (slideMargin * 2)) * slider.count) - slider.w - slideMargin : ((slider.itemW + slideMargin) * slider.count) - slider.w;
      } else {
        slider.itemW = slider.w;
        slider.pagingCount = slider.count;
        slider.last = slider.count - 1;
      }
      slider.computedW = slider.itemW - slider.boxPadding;
    }
    
    slider.update = function(pos, action) {
      slider.doMath();
      
      // update currentSlide and slider.animatingTo if necessary
      if (!carousel) {
        if (pos < slider.currentSlide) {
          slider.currentSlide += 1;
        } else if (pos <= slider.currentSlide && pos !== 0) {
          slider.currentSlide -= 1;
        }
        slider.animatingTo = slider.currentSlide;
      }
      
      // update controlNav
      if (vars.controlNav && !slider.manualControls) {
        if ((action === "add" && !carousel) || slider.pagingCount > slider.controlNav.length) {
          methods.controlNav.update("add");
        } else if ((action === "remove" && !carousel) || slider.pagingCount < slider.controlNav.length) {
          if (carousel && slider.currentSlide > slider.last) {
            slider.currentSlide -= 1;
            slider.animatingTo -= 1;
          }
          methods.controlNav.update("remove", slider.last);
        }
      }
      // update directionNav
      if (vars.directionNav) methods.directionNav.update();
      
    }
    
    slider.addSlide = function(obj, pos) {
      var $obj = $(obj);
      
      slider.count += 1;
      slider.last = slider.count - 1;
      
      // append new slide
      if (vertical && reverse) {
        (pos !== undefined) ? slider.slides.eq(slider.count - pos).after($obj) : slider.container.prepend($obj);
      } else {
        (pos !== undefined) ? slider.slides.eq(pos).before($obj) : slider.container.append($obj);
      }
      
      // update currentSlide, animatingTo, controlNav, and directionNav
      slider.update(pos, "add");
      
      // update slider.slides
      slider.slides = $(vars.selector + ':not(.clone)', slider);
      // re-setup the slider to accomdate new slide
      slider.setup();
      
      //FlexSlider: added() Callback
      vars.added(slider);
    }
    slider.removeSlide = function(obj) {
      var pos = (isNaN(obj)) ? slider.slides.index($(obj)) : obj;
      
      // update count
      slider.count -= 1;
      slider.last = slider.count - 1;
      
      // remove slide
      if (isNaN(obj)) {
        $(obj, slider.slides).remove();
      } else {
        (vertical && reverse) ? slider.slides.eq(slider.last).remove() : slider.slides.eq(obj).remove();
      }
      
      // update currentSlide, animatingTo, controlNav, and directionNav
      slider.doMath();
      slider.update(pos, "remove");
      
      // update slider.slides
      slider.slides = $(vars.selector + ':not(.clone)', slider);
      // re-setup the slider to accomdate new slide
      slider.setup();
      
      // FlexSlider: removed() Callback
      vars.removed(slider);
    }
    
    //FlexSlider: Initialize
    methods.init();
  }
  
  //FlexSlider: Default Settings
  $.flexslider.defaults = {
    namespace: "flex-",             //{NEW} String: Prefix string attached to the class of every element generated by the plugin
    selector: ".slides > li",       //{NEW} Selector: Must match a simple pattern. '{container} > {slide}' -- Ignore pattern at your own peril
    animation: "fade",              //String: Select your animation type, "fade" or "slide"
    easing: "swing",               //{NEW} String: Determines the easing method used in jQuery transitions. jQuery easing plugin is supported!
    direction: "horizontal",        //String: Select the sliding direction, "horizontal" or "vertical"
    reverse: false,                 //{NEW} Boolean: Reverse the animation direction
    animationLoop: true,             //Boolean: Should the animation loop? If false, directionNav will received "disable" classes at either end
    smoothHeight: false,            //{NEW} Boolean: Allow height of the slider to animate smoothly in horizontal mode  
    startAt: 0,                     //Integer: The slide that the slider should start on. Array notation (0 = first slide)
    slideshow: true,                //Boolean: Animate slider automatically
    slideshowSpeed: 7000,           //Integer: Set the speed of the slideshow cycling, in milliseconds
    animationSpeed: 600,            //Integer: Set the speed of animations, in milliseconds
    initDelay: 0,                   //{NEW} Integer: Set an initialization delay, in milliseconds
    randomize: false,               //Boolean: Randomize slide order
    
    // Usability features
    pauseOnAction: true,            //Boolean: Pause the slideshow when interacting with control elements, highly recommended.
    pauseOnHover: false,            //Boolean: Pause the slideshow when hovering over slider, then resume when no longer hovering
    useCSS: true,                   //{NEW} Boolean: Slider will use CSS3 transitions if available
    touch: true,                    //{NEW} Boolean: Allow touch swipe navigation of the slider on touch-enabled devices
    video: false,                   //{NEW} Boolean: If using video in the slider, will prevent CSS3 3D Transforms to avoid graphical glitches
    
    // Primary Controls
    controlNav: true,               //Boolean: Create navigation for paging control of each clide? Note: Leave true for manualControls usage
    directionNav: true,             //Boolean: Create navigation for previous/next navigation? (true/false)
    prevText: "Previous",           //String: Set the text for the "previous" directionNav item
    nextText: "Next",               //String: Set the text for the "next" directionNav item
    
    // Secondary Navigation
    keyboard: true,                 //Boolean: Allow slider navigating via keyboard left/right keys
    multipleKeyboard: false,        //{NEW} Boolean: Allow keyboard navigation to affect multiple sliders. Default behavior cuts out keyboard navigation with more than one slider present.
    mousewheel: false,              //{UPDATED} Boolean: Requires jquery.mousewheel.js (https://github.com/brandonaaron/jquery-mousewheel) - Allows slider navigating via mousewheel
    pausePlay: false,               //Boolean: Create pause/play dynamic element
    pauseText: "Pause",             //String: Set the text for the "pause" pausePlay item
    playText: "Play",               //String: Set the text for the "play" pausePlay item
    
    // Special properties
    controlsContainer: "",          //{UPDATED} jQuery Object/Selector: Declare which container the navigation elements should be appended too. Default container is the FlexSlider element. Example use would be $(".flexslider-container"). Property is ignored if given element is not found.
    manualControls: "",             //{UPDATED} jQuery Object/Selector: Declare custom control navigation. Examples would be $(".flex-control-nav li") or "#tabs-nav li img", etc. The number of elements in your controlNav should match the number of slides/tabs.
    sync: "",                       //{NEW} Selector: Mirror the actions performed on this slider with another slider. Use with care.
    asNavFor: "",                   //{NEW} Selector: Internal property exposed for turning the slider into a thumbnail navigation for another slider
    
    // Carousel Options
    itemWidth: 0,                   //{NEW} Integer: Box-model width of individual carousel items, including horizontal borders and padding.
    itemMargin: 0,                  //{NEW} Integer: Margin between carousel items.
    minItems: 0,                    //{NEW} Integer: Minimum number of carousel items that should be visible. Items will resize fluidly when below this.
    maxItems: 0,                    //{NEW} Integer: Maxmimum number of carousel items that should be visible. Items will resize fluidly when above this limit.
    move: 0,                        //{NEW} Integer: Number of carousel items that should move on animation. If 0, slider will move all visible items.
                                    
    // Callback API
    start: function(){},            //Callback: function(slider) - Fires when the slider loads the first slide
    before: function(){},           //Callback: function(slider) - Fires asynchronously with each slider animation
    after: function(){},            //Callback: function(slider) - Fires after each slider animation completes
    end: function(){},              //Callback: function(slider) - Fires when the slider reaches the last slide (asynchronous)
    added: function(){},            //{NEW} Callback: function(slider) - Fires after a slide is added
    removed: function(){}           //{NEW} Callback: function(slider) - Fires after a slide is removed
  }

  //FlexSlider: Plugin Function
  $.fn.flexslider = function(options) {
    options = options || {};
    if (typeof options === "object") {
      return this.each(function() {
        var $this = $(this),
            selector = (options.selector) ? options.selector : ".slides > li",
            $slides = $this.find(selector);

        if ($slides.length === 1) {
          $slides.fadeIn(400);
          if (options.start) options.start($this);
        } else if ($this.data('flexslider') === undefined) {
          new $.flexslider(this, options);
        }
      });
    } else {
      // Helper strings to quickly perform functions on the slider
      var $slider = $(this).data('flexslider');
      if (!$slider) {
        return; // or throw an error, or handle the case where the slider isn't initialized
      }
      switch (options) {
        case "play": $slider.play(); break;
        case "pause": $slider.pause(); break;
        case "next": $slider.flexAnimate($slider.getTarget("next"), true); break;
        case "prev":
        case "previous": $slider.flexAnimate($slider.getTarget("prev"), true); break;
        default: if (typeof options === "number") $slider.flexAnimate(options, true);
      }
    }
  }  
})(jQuery);
